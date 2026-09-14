import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'config.dart';
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

class _Retryable implements Exception {
  final String message;
  _Retryable(this.message);
}

/// Groq chat with tool calling, streamed. Runs entirely on the phone.
class Brain {
  final AnviConfig cfg;
  final http.Client client;
  final Tools tools;
  final _history = <Map<String, dynamic>>[];
  int _turn = 0;
  static const _maxHistory = 16;
  static const _tokenLimit = 7600; // Groq free tier: ~8k tokens/minute
  static const _inputBudget = 4200;

  Brain(this.cfg, this.client, this.tools);

  void reset() => _history.clear();

  List<Map<String, dynamic>> get _tools => [...phoneTools, if (cfg.hasPc) ...pcTools];

  String _systemPrompt(String location) {
    final now = DateTime.now();
    const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
    const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
      'October', 'November', 'December'];
    final hour = now.hour % 12 == 0 ? 12 : now.hour % 12;
    final time = '${days[now.weekday - 1]}, ${now.day} ${months[now.month - 1]} ${now.year}, '
        '$hour:${now.minute.toString().padLeft(2, '0')} ${now.hour < 12 ? 'AM' : 'PM'}';
    return 'You are ANVI, a smart, warm personal voice assistant running as an app on the user\'s Android phone. '
        'Your replies are spoken aloud: answer in 1-3 short natural sentences (under 60 words unless the user asks '
        'for detail), with no markdown, bullet points, numbered lists, emojis, tables or URLs. Write numbers as '
        'digits with units (like ₹15,458 per gram or 27°C); they are read aloud correctly. '
        'For anything that changes over time or that you are not sure about - prices and rates, news, sports, '
        'events, releases, people\'s current roles - call web_search first and answer with the specific facts and '
        'numbers you found, briefly naming the source. Never say you don\'t know or can\'t browse without searching '
        'first. '
        '${cfg.hasPc ? 'You can also control the user\'s Windows PC with the PC tools; those actions happen on the PC, so say "on your PC". Only take PC actions the user asked for in their latest message. ' : 'PC control is not set up in this app yet. '}'
        'Never claim you did something unless a tool call in this turn actually did it and succeeded; if no tool '
        'can do it, say honestly that you can\'t do that yet. '
        'When the user asks for code, write complete working code in fenced code blocks with the language and a '
        'filename on the opening fence line, like ```python hello.py; the code is shown on screen, not read aloud, '
        'so outside the code write only one short sentence. '
        'Current local date and time: $time. User\'s location: $location.';
  }

  int _estimate(List<Map<String, dynamic>> msgs) =>
      msgs.fold<int>(0, (n, m) => n + jsonEncode(m).length) ~/ 3 + jsonEncode(_tools).length ~/ 3;

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

  /// One conversation turn: streams text deltas and tool notifications.
  Stream<BrainEvent> ask(String userText) async* {
    _turn++;
    _history.add({'role': 'user', 'content': userText});
    final convo = await _conversation();
    final executed = <String>{};
    var finalText = '';
    var noTools = false;

    try {
      for (var step = 0; step < 5; step++) {
        final calls = <int, Map<String, String>>{};
        var content = '';
        await for (final delta in _streamWithFallback(convo, step < 4 && !noTools)) {
          final piece = delta['content'];
          if (piece is String && piece.isNotEmpty) {
            final text = content.isEmpty && finalText.isNotEmpty && !finalText.endsWith(' ') ? ' $piece' : piece;
            content += text;
            yield TextDelta(text);
          }
          for (final tc in (delta['tool_calls'] as List? ?? [])) {
            final slot = calls.putIfAbsent(tc['index'] ?? calls.length, () => {'id': '', 'name': '', 'args': ''});
            if (tc['id'] != null) slot['id'] = '${tc['id']}';
            final fn = tc['function'] ?? {};
            slot['name'] = slot['name']! + (fn['name'] ?? '');
            slot['args'] = slot['args']! + (fn['arguments'] ?? '');
          }
        }

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
            for (final c in fresh)
              {
                'id': c['id'],
                'type': 'function',
                'function': {'name': c['name'], 'arguments': c['args']!.isEmpty ? '{}' : c['args']},
              }
          ],
        });
        for (final c in fresh) {
          Map<String, dynamic> args;
          try {
            args = Map<String, dynamic>.from(jsonDecode(c['args']!.isEmpty ? '{}' : c['args']!));
          } catch (_) {
            args = {};
          }
          yield ToolStarted(c['name']!, args);
          final result = jsonEncode(await tools.run(c['name']!, args, userText, _turn));
          convo.add({
            'role': 'tool',
            'tool_call_id': c['id'],
            'content': result.length > 4500 ? result.substring(0, 4500) : result,
          });
        }
      }
      if (finalText.trim().isEmpty) {
        finalText = "Sorry, I couldn't come up with an answer. Could you ask that again?";
        yield TextDelta(finalText);
      }
    } finally {
      // close the turn even if interrupted, so it isn't acted on again next time
      _history.add({
        'role': 'assistant',
        'content': finalText.trim().isEmpty
            ? '[interrupted before replying; do not act on that request again unless asked]'
            : finalText,
      });
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

  Stream<Map<String, dynamic>> _streamWithFallback(List<Map<String, dynamic>> msgs, bool useTools) async* {
    final models = [cfg.groqModel, if (cfg.fallbackModel != cfg.groqModel) cfg.fallbackModel];
    for (final model in models) {
      try {
        yield* _stream(msgs, useTools, model);
        return;
      } on _Retryable {
        continue;
      }
    }
    throw AnviError("I've hit the Groq rate limit. Give me a minute and ask again.");
  }

  Stream<Map<String, dynamic>> _stream(List<Map<String, dynamic>> msgs, bool useTools, String model) async* {
    final budget = (_tokenLimit - _estimate(msgs)).clamp(400, 3000);
    final request = http.Request('POST', Uri.parse('https://api.groq.com/openai/v1/chat/completions'))
      ..headers.addAll({'Authorization': 'Bearer ${cfg.groqKey}', 'Content-Type': 'application/json'})
      ..body = jsonEncode({
        'model': model,
        'messages': msgs,
        'temperature': 0.6,
        'max_completion_tokens': budget,
        'stream': true,
        if (model.startsWith('openai/gpt-oss')) 'reasoning_effort': 'low',
        if (useTools) 'tools': _tools,
        if (useTools) 'tool_choice': 'auto',
      });
    final response = await client.send(request).timeout(const Duration(seconds: 30));
    if (response.statusCode != 200) {
      final body = await response.stream.bytesToString();
      if (response.statusCode == 413 || response.statusCode == 429 || body.contains('tool_use_failed')) {
        throw _Retryable('${response.statusCode}');
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
