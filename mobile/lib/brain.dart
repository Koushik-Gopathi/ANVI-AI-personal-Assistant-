import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'dart:io' show SocketException, HandshakeException;

import 'config.dart';
import 'store.dart';
import 'tools.dart';
import 'voice.dart';

sealed class BrainEvent {}

class TextDelta extends BrainEvent {
  final String text;
  TextDelta(this.text);
}

class ToolStarted extends BrainEvent {
  final String name;
  final Map<String, dynamic> args;
  ToolStarted(this.name, this.args);
}

/// A finished tool step, for the activity list.
class StepDone extends BrainEvent {
  final String text;
  final String outcome; // done | failed | waiting for your OK
  StepDone(this.text, this.outcome);
}

class StatusUpdate extends BrainEvent {
  final String text;
  StatusUpdate(this.text);
}

class _Retryable implements Exception {
  final String message;
  final Duration wait;
  _Retryable(this.message, this.wait);
}

// For requests to *do* something, text written before any tool ran is held back:
// if it claims success without a tool call, it is dropped and the model is told to act.
final _actionRequest = RegExp(
    r'\b(delete|remove|create|make|open|close|run|install|move|copy|rename|write|save|send|type|set|start|stop|'
    r'turn|change|download|lock|shut|restart|play|pause|call|message|text|whatsapp|remind|alarm|timer|navigate)\b',
    caseSensitive: false);
final _claimsDone = RegExp(
    r'\b(done|deleted|removed|created|made|opened|closed|ran|installed|moved|copied|renamed|written|wrote|saved|'
    r'sent|typed|started|stopped|turned|changed|downloaded|locked|playing|paused|called|has been|have been|is now)\b',
    caseSensitive: false);

/// Groq chat with tool calling, streamed. Runs on the phone; PC actions go through the PC.
class Brain {
  final AnviConfig cfg;
  final http.Client client;
  final Tools tools;
  final _history = <Map<String, dynamic>>[];
  int _turn = 0;
  static const _maxHistory = 16;
  static const _maxSteps = 12;
  static const _tokenLimit = 7600; // Groq free tier: ~8k tokens/minute per model
  static const _inputBudget = 5400;
  final _freeAt = <String, DateTime>{};

  Brain(this.cfg, this.client, this.tools);

  void reset() => _history.clear();

  bool _usePc = false;
  bool _lastTurnUsedPc = false;
  static final _pcRequest = RegExp(r'\b(laptop|pc|computer|desktop|windows|system)\b', caseSensitive: false);
  static final _shortReply = RegExp(r'^\s*(yes|yeah|yep|ok|okay|sure|no|cancel|do it|go ahead)\b', caseSensitive: false);

  List<Map<String, dynamic>> get _tools => [...phoneTools, if (_usePc) ...tools.pc.toolDefs];

  String _systemPrompt(String location) {
    final now = DateTime.now();
    const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
    const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
      'October', 'November', 'December'];
    final hour = now.hour % 12 == 0 ? 12 : now.hour % 12;
    final time = '${days[now.weekday - 1]}, ${now.day} ${months[now.month - 1]} ${now.year}, '
        '$hour:${now.minute.toString().padLeft(2, '0')} ${now.hour < 12 ? 'AM' : 'PM'}';
    final store = Store.instance;
    final pc = _usePc && tools.pc.toolDefs.isNotEmpty
        ? 'Tools marked [on the PC] act on the user\'s Windows laptop (commands, files, documents, apps, typing, '
            'reminders); use them only for things the user wants done on the laptop, and say "on your laptop". '
        : _usePc && cfg.hasPc
            ? 'The user\'s laptop is not reachable right now (Karen must be open on it, on the same network); say so. '
            : 'Everything happens on this phone unless the user mentions their laptop or PC. ';
    const languageRules = {
      'english': 'Reply in English. ',
      'telugu': 'Reply in Telugu written in Telugu script, unless the user clearly wants English. Keep names and '
          'technical terms as they are. ',
      'hindi': 'Reply in Hindi written in Devanagari script, unless the user clearly wants English. Keep names and '
          'technical terms as they are. ',
      'auto': 'Reply in the same language the user used (Telugu in Telugu script, Hindi in Devanagari). ',
    };
    return 'You are ${store.wakeWord}, the user\'s personal AI agent, running as an app on their Android phone. '
        'You don\'t just chat: you get things done with your tools - opening phone apps, calling and messaging '
        'contacts, reading notifications, calendar, alarms, timers, flashlight, volume, maps, web search, speed '
        'tests, remembering things, and work on their laptop. '
        'For people, look up the contact with find_contact (or pass the name to call/message tools). '
        'For a task: work out the steps, call tools one after another, read every result, fix errors by trying '
        'another way, and keep going until it is really finished. If a tool result says needs_confirmation, stop '
        'and ask the user, describing exactly what will happen. Never claim you did something unless a tool result '
        'shows it worked. Messages and calls only get prepared: tell the user to tap send or call. '
        '$pc'
        'For anything that changes over time or that you are unsure of - prices, news, sports, releases, people\'s '
        'roles - search the web first and answer with the specific facts and numbers you found. '
        'Your final reply is spoken aloud: 1-3 short natural sentences (under 60 words unless asked for detail), '
        'no markdown, lists, emojis, tables or URLs. Write numbers as digits with units (like ₹15,458 or 27°C). '
        'When the user asks for code, write complete code in fenced code blocks with the language and a filename on '
        'the opening fence line, like ```python hello.py; outside the code write one short sentence. '
        '${languageRules[store.language]}'
        'When the user tells you something to remember, use remember. '
        '${store.memoryPrompt()}'
        'Current local date and time: $time. User\'s location: $location.';
  }

  int _estimate(List<Map<String, dynamic>> msgs) =>
      msgs.fold<int>(0, (n, m) => n + jsonEncode(m).length) ~/ 3 + jsonEncode(_tools).length ~/ 4;

  static final fence = RegExp(r'```([^\n`]*)\n(.*?)```', dotAll: true);

  Future<List<Map<String, dynamic>>> _conversation() async {
    final msgs = _history.map((m) => Map<String, dynamic>.from(m)).toList();
    final lastAssistant = msgs.lastIndexWhere((m) => m['role'] == 'assistant');
    for (var i = 0; i < msgs.length; i++) {
      if (msgs[i]['role'] == 'assistant' && i != lastAssistant) {
        msgs[i]['content'] = '${msgs[i]['content']}'.replaceAll(fence, '[code omitted]');
      }
    }
    final home = await tools.homeLocation();
    final system = {'role': 'system', 'content': _systemPrompt(home?['name'] ?? 'unknown')};
    while (msgs.length > 1 && _estimate([system, ...msgs]) > _inputBudget) {
      msgs.removeAt(0);
    }
    return [system, ...msgs];
  }

  /// Keep a long task under the token budget: shorten old tool results, then drop old chat.
  void _compact(List<Map<String, dynamic>> convo, int turnStart) {
    if (_estimate(convo) <= _inputBudget) return;
    final toolMsgs = convo.skip(turnStart).where((m) => m['role'] == 'tool').toList();
    for (final m in toolMsgs.take(toolMsgs.length > 2 ? toolMsgs.length - 2 : 0)) {
      final c = '${m['content']}';
      if (c.length > 300) m['content'] = '${c.substring(0, 300)}… [shortened]';
    }
    while (_estimate(convo) > _inputBudget && turnStart > 2) {
      convo.removeAt(1);
      turnStart--;
    }
  }

  static String _outcome(String name, Map<String, dynamic> result) {
    if (result['needs_confirmation'] == true) return 'waiting for your OK';
    if (result['error'] != null) return 'failed';
    if (name == 'run_command' && result['exit_code'] != null && result['exit_code'] != 0) {
      return 'exit code ${result['exit_code']}';
    }
    return 'done';
  }

  /// One conversation turn: streams text, tool, step and status events.
  _TurnState? _openTurn;

  Stream<BrainEvent> ask(String userText) async* {
    _turn++;
    // the user spoke over the previous answer, which may still be finishing: close it off first,
    // so this question isn't mixed up with the old one
    final previous = _openTurn;
    if (previous != null && !previous.closed) {
      previous
        ..closed = true
        ..cancelled = true;
      _history.add({'role': 'assistant', 'content': "[the user interrupted this answer; don't continue it unless asked]"});
    }
    final me = _openTurn = _TurnState();
    _usePc = cfg.hasPc && (_pcRequest.hasMatch(userText) || (_lastTurnUsedPc && _shortReply.hasMatch(userText)));
    if (_usePc) await tools.pc.refreshToolDefs();
    final steps = <(String, String)>[];
    var usedPc = false;
    final lastReply = _history.lastWhere((m) => m['role'] == 'assistant', orElse: () => {'content': ''})['content'];
    _history.add({'role': 'user', 'content': userText});
    final convo = await _conversation();
    final turnStart = convo.length - 1;
    final executed = <String>{};
    final wantsAction = _actionRequest.hasMatch(userText);
    var finalText = '';
    var noTools = false;
    var nudged = false;

    try {
      for (var step = 0; step < _maxSteps; step++) {
        _compact(convo, turnStart);
        final calls = <int, Map<String, String>>{};
        var content = '';
        final hold = wantsAction && executed.isEmpty && !nudged;
        await for (final delta in _streamWithFallback(convo, step < _maxSteps - 1 && !noTools)) {
          if (me.cancelled) return;
          if (delta.containsKey('_wait')) {
            yield StatusUpdate('${delta['_why'] ?? 'busy'}, continuing in ${delta['_wait']}s');
            continue;
          }
          final piece = delta['content'];
          if (piece is String && piece.isNotEmpty) {
            final text = content.isEmpty && finalText.isNotEmpty && !finalText.endsWith(' ') ? ' $piece' : piece;
            content += text;
            if (!hold) yield TextDelta(text);
          }
          for (final tc in (delta['tool_calls'] as List? ?? [])) {
            final slot = calls.putIfAbsent(tc['index'] ?? calls.length, () => {'id': '', 'name': '', 'args': ''});
            if (tc['id'] != null) slot['id'] = '${tc['id']}';
            final fn = tc['function'] ?? {};
            slot['name'] = slot['name']! + (fn['name'] ?? '');
            slot['args'] = slot['args']! + (fn['arguments'] ?? '');
          }
        }

        if (hold && calls.isEmpty && _claimsDone.hasMatch(content)) {
          nudged = true;
          convo.add({
            'role': 'system',
            'content': 'You have not called any tool in this turn, so nothing has actually been done. Call the right '
                'tool now to do what the user asked, or say honestly that you have not done it.',
          });
          continue;
        }
        if (hold && content.isNotEmpty) yield TextDelta(content);

        final fresh = calls.values.where((c) => !executed.contains(_key(c))).toList();
        if (calls.isNotEmpty && fresh.isEmpty) {
          if (content.trim().isNotEmpty) {
            finalText = content;
            break;
          }
          noTools = true; // looping on the same call: make it answer
          continue;
        }
        finalText += content;
        if (fresh.isEmpty) break;
        executed.addAll(fresh.map(_key));

        convo.add({
          'role': 'assistant',
          'content': content,
          'tool_calls': [
            for (var i = 0; i < fresh.length; i++)
              {
                'id': fresh[i]['id']!.isEmpty ? 'call_${step}_$i' : fresh[i]['id'],
                'type': 'function',
                'function': {'name': fresh[i]['name'], 'arguments': fresh[i]['args']!.isEmpty ? '{}' : fresh[i]['args']},
              }
          ],
        });
        for (var i = 0; i < fresh.length; i++) {
          final c = fresh[i];
          Map<String, dynamic> args;
          try {
            final decoded = jsonDecode(c['args']!.isEmpty ? '{}' : c['args']!);
            args = decoded is Map ? Map<String, dynamic>.from(decoded) : {};
          } catch (_) {
            args = {};
          }
          if (me.cancelled) return; // interrupted: don't start more actions
          yield ToolStarted(c['name']!, args);
          final result = await tools.run(c['name']!, args, userText, _turn, '$lastReply');
          final step = (toolStatus(c['name']!, args), _outcome(c['name']!, result));
          steps.add(step);
          if (tools.pc.toolDefs.any((t) => t['function']['name'] == c['name'])) usedPc = true;
          yield StepDone(step.$1, step.$2);
          final text = jsonEncode(result);
          convo.add({
            'role': 'tool',
            'tool_call_id': c['id']!.isEmpty ? 'call_${step}_$i' : c['id'],
            'content': text.length > 4000 ? text.substring(0, 4000) : text,
          });
        }
      }
      if (finalText.trim().isEmpty) {
        finalText = "Sorry, I couldn't finish that. Could you ask again?";
        yield TextDelta(finalText);
      }
    } finally {
      _lastTurnUsedPc = usedPc;
      if (finalText.trim().isNotEmpty && !me.cancelled) {
        Store.instance.logExchange(userText, finalText.replaceAll(fence, '[code]').trim(), steps);
      }
      // close the turn even if interrupted, so it isn't acted on again next time
      if (!me.closed) {
        _history.add({
          'role': 'assistant',
          'content': finalText.trim().isEmpty
              ? '[interrupted before replying; do not act on that request again unless asked]'
              : finalText,
        });
      }
      me.closed = true;
      while (_history.length > _maxHistory) {
        _history.removeAt(0);
      }
    }
  }

  static String _key(Map<String, String> call) {
    try {
      final args = Map<String, dynamic>.from(jsonDecode(call['args']!.isEmpty ? '{}' : call['args']!))
        ..removeWhere((_, v) => v == null || v == '');
      final sorted = Map.fromEntries(args.entries.toList()..sort((a, b) => a.key.compareTo(b.key)));
      return '${call['name']}|${jsonEncode(sorted)}';
    } catch (_) {
      return '${call['name']}|${call['args']}';
    }
  }

  /// Tries each model; when all are rate limited, waits (up to 45 s) for the first to free up.
  Stream<Map<String, dynamic>> _streamWithFallback(List<Map<String, dynamic>> msgs, bool useTools) async* {
    final models = [cfg.groqModel, if (cfg.fallbackModel != cfg.groqModel) cfg.fallbackModel];
    var networkFailures = 0;
    for (var round = 0; round < 6; round++) {
      for (final model in models) {
        if ((_freeAt[model] ?? DateTime(2000)).isAfter(DateTime.now())) continue;
        var started = false;
        try {
          await for (final delta in _stream(msgs, useTools, model)) {
            started = true;
            yield delta;
          }
          return;
        } on _Retryable catch (e) {
          if (started) rethrow;
          _freeAt[model] = DateTime.now().add(e.wait);
        } on Exception catch (e) {
          // slow or dropped connection: wait a little and try again
          if (started || !_isNetworkError(e)) rethrow;
          networkFailures++;
          if (networkFailures >= 3) {
            throw AnviError('The internet is slow right now and I couldn\'t reach the AI. Please try again.');
          }
          yield {'_wait': 2 * networkFailures, '_why': 'reconnecting'};
          await Future.delayed(Duration(seconds: 2 * networkFailures));
          break; // retry from the first available model
        }
      }
      final soonest = models.map((m) => _freeAt[m] ?? DateTime.now()).reduce((a, b) => a.isBefore(b) ? a : b);
      final wait = soonest.difference(DateTime.now());
      if (wait > const Duration(seconds: 45)) break;
      if (wait > Duration.zero) {
        yield {'_wait': wait.inSeconds + 1};
        await Future.delayed(wait + const Duration(milliseconds: 500));
      }
    }
    throw AnviError("I've hit the free Groq limit for now. Give me a minute and ask again.");
  }

  static bool _isNetworkError(Object e) =>
      e is TimeoutException || e is SocketException || e is HandshakeException || e is http.ClientException;

  static Duration _retryAfter(http.StreamedResponse response, String body) {
    final header = double.tryParse(response.headers['retry-after'] ?? '');
    if (header != null) return Duration(milliseconds: (header * 1000).round());
    final m = RegExp(r'try again in (?:(\d+)m)?([\d.]+)s').firstMatch(body);
    if (m == null) return const Duration(seconds: 20);
    final seconds = int.parse(m.group(1) ?? '0') * 60 + double.parse(m.group(2)!);
    return Duration(milliseconds: (seconds * 1000).round());
  }

  Stream<Map<String, dynamic>> _stream(List<Map<String, dynamic>> msgs, bool useTools, String model) async* {
    final budget = (_tokenLimit - _estimate(msgs)).clamp(600, 3000);
    final request = http.Request('POST', Uri.parse('https://api.groq.com/openai/v1/chat/completions'))
      ..headers.addAll({'Authorization': 'Bearer ${cfg.groqKey}', 'Content-Type': 'application/json'})
      ..body = jsonEncode({
        'model': model,
        'messages': msgs,
        'temperature': 0.5,
        'max_completion_tokens': budget,
        'stream': true,
        if (model.startsWith('openai/gpt-oss')) 'reasoning_effort': 'low',
        if (useTools) 'tools': _tools,
        if (useTools) 'tool_choice': 'auto',
      });
    final response = await client.send(request).timeout(const Duration(seconds: 45));
    if (response.statusCode != 200) {
      final body = await response.stream.bytesToString();
      if (response.statusCode == 429) throw _Retryable('429', _retryAfter(response, body));
      if (response.statusCode == 413 || response.statusCode == 503 || body.contains('tool_use_failed')) {
        throw _Retryable('${response.statusCode}', const Duration(seconds: 2));
      }
      throw AnviError('Groq error ${response.statusCode}');
    }
    final lines = response.stream.transform(utf8.decoder).transform(const LineSplitter());
    await for (final line in lines) {
      if (!line.startsWith('data:')) continue;
      final data = line.substring(5).trim();
      if (data == '[DONE]') break;
      final chunk = jsonDecode(data);
      if (chunk['error'] != null) throw AnviError('Groq: ${chunk['error']}');
      final choices = chunk['choices'] as List? ?? [];
      if (choices.isNotEmpty) yield Map<String, dynamic>.from(choices.first['delta'] ?? {});
    }
  }
}

class _TurnState {
  bool closed = false;
  bool cancelled = false;
}
