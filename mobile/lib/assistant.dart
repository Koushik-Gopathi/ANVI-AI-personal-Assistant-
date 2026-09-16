import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:wakelock_plus/wakelock_plus.dart';

import 'brain.dart';
import 'config.dart';
import 'orb.dart';
import 'store.dart';
import 'tools.dart';
import 'voice.dart';

class CodeBlock {
  final String lang;
  final String? filename;
  final String code;
  CodeBlock(this.lang, this.filename, this.code);
}

// "Karen" as speech-to-text may spell it (Karen, Caren, Karan, Karin, Keren...)
final _karenRe = RegExp(r'\b[kc](?:a|e|ae|ai)r+(?:e|a|i|y)n+\b', caseSensitive: false);
RegExp? _customRe;
String _customFor = '';

/// Matches the wake word chosen in settings (fuzzy for "Karen").
RegExp get _wakeRe {
  final word = Store.instance.wakeWord.toLowerCase().replaceAll(RegExp(r'[^a-z\s]'), ' ').trim();
  if (word.isEmpty || word == 'karen') return _karenRe;
  if (word != _customFor) {
    _customFor = word;
    _customRe = RegExp('\\b${word.split(RegExp(r'\s+')).map(RegExp.escape).join(r'\s*')}\\b', caseSensitive: false);
  }
  return _customRe!;
}
const _sleepWords = {
  'hey', 'hi', 'ok', 'okay', 'go', 'to', 'sleep', 'bye', 'by', 'goodbye', 'good', 'night', 'stop', 'thanks', 'thank',
  'you', 'that', 's', 'all', 'please', 'now', 'shut', 'down', 'standby', 'pause', 'the', 'a'
};

String _normalize(String t) => ' ${t.toLowerCase().replaceAll(RegExp(r'[^a-z\s]'), ' ').replaceAll(RegExp(r'\s+'), ' ')} ';
bool hasWakeWord(String t) => _wakeRe.hasMatch(_normalize(t));

/// "Karen", "bye Karen", "Karen go to sleep" -> true;  "Karen what's the time" -> false
bool isSleepCommand(String t) {
  final n = _normalize(t);
  if (!_wakeRe.hasMatch(n)) return false;
  return n.replaceAll(_wakeRe, ' ').trim().split(' ').where((w) => w.isNotEmpty && !_sleepWords.contains(w)).isEmpty;
}

/// "Karen, what's the time?" -> "what's the time?"
String afterWakeWord(String t) {
  final m = _wakeRe.firstMatch(t);
  if (m == null) return '';
  final rest = t.substring(m.end).replaceFirst(RegExp(r'^[\s,.!?:;-]+'), '').trim();
  return rest.split(RegExp(r'\s+')).where((w) => w.isNotEmpty).length >= 2 ? rest : '';
}

/// Runs a conversation: wake word, listening, thinking, speaking.
class Assistant extends ChangeNotifier {
  final AnviConfig cfg;
  final http.Client client = http.Client();
  late final Deepgram deepgram = Deepgram(cfg, client);
  late final Tools tools = Tools(cfg, client);
  late final Brain brain = Brain(cfg, client, tools);
  final mic = MicListener();
  final player = SpeechPlayer();

  OrbMode mode = OrbMode.sleep;
  bool awake = false;
  bool passive = false; // asleep but listening for "Karen"
  bool paused = false; // app in background
  String you = '';
  String reply = '';
  String detail = '';
  String? error;
  List<CodeBlock> code = [];
  final steps = <(String, String)>[]; // what Karen did this turn: (text, outcome)
  final _codeShown = StreamController<List<CodeBlock>>.broadcast();
  Stream<List<CodeBlock>> get onCode => _codeShown.stream;

  int _gen = 0;
  bool _checkingWake = false;
  Timer? _errorTimer;

  Assistant(this.cfg) {
    player.onStart = () {
      if (mode == OrbMode.thinking) _setMode(OrbMode.speaking);
    };
    player.onProblem = _showError;
  }

  String get status {
    if (error != null) return error!;
    return switch (mode) {
      OrbMode.sleep => passive ? 'say “${Store.instance.wakeWord}” to wake' : 'tap to wake',
      OrbMode.listening => 'listening...',
      OrbMode.thinking => detail.isNotEmpty ? '${detail.replaceAll(RegExp(r'\.+$'), '')}...' : 'thinking...',
      OrbMode.speaking => 'speaking...',
    };
  }

  String get hint => switch (mode) {
        OrbMode.sleep => passive ? 'or tap the orb' : 'tap the orb',
        OrbMode.listening => 'speak naturally · say “${Store.instance.wakeWord}” to sleep',
        OrbMode.thinking => 'tap to cancel',
        OrbMode.speaking => 'tap to interrupt',
      };

  double get level => mic.level;

  void _setMode(OrbMode m) {
    mode = m;
    notifyListeners();
  }

  void _showError(String message) {
    error = message.length > 90 ? '${message.substring(0, 90)}…' : message;
    notifyListeners();
    _errorTimer?.cancel();
    _errorTimer = Timer(const Duration(seconds: 4), () {
      error = null;
      notifyListeners();
    });
  }

  // --- sleep / wake ---------------------------------------------------------
  Future<void> startPassive() async {
    if (awake || paused) return;
    mic.asleep = true;
    mic.onUtterance = _onWakeCandidate;
    passive = await mic.start();
    if (!passive) _showError('${Store.instance.wakeWord} needs microphone permission');
    notifyListeners();
  }

  Future<void> _onWakeCandidate(Uint8List wav) async {
    if (awake || _checkingWake) return;
    _checkingWake = true;
    try {
      final text = await deepgram.transcribe(wav, wake: true);
      if (!awake && hasWakeWord(text) && !(isSleepCommand(text) && afterWakeWord(text).isNotEmpty)) {
        await wakeUp(afterWakeWord(text));
      }
    } catch (_) {
      // offline for a moment: keep listening
    } finally {
      _checkingWake = false;
    }
  }

  Future<void> wakeUp([String request = '']) async {
    if (awake) return;
    awake = true;
    passive = false;
    HapticFeedback.mediumImpact();
    WakelockPlus.enable();
    if (request.isNotEmpty) {
      await mic.stop();
      return ask(request);
    }
    await listen();
  }

  Future<void> goToSleep() async {
    _gen++;
    await player.stop();
    awake = false;
    detail = '';
    HapticFeedback.lightImpact();
    WakelockPlus.disable();
    _setMode(OrbMode.sleep);
    await mic.stop();
    await startPassive();
  }

  Future<void> _afterTurn() async {
    if (awake) {
      await listen();
    } else {
      _setMode(OrbMode.sleep);
      await startPassive();
    }
  }

  /// Orb tap: interrupt, wake up, or go to sleep.
  Future<void> tap() async {
    if (mode == OrbMode.thinking || mode == OrbMode.speaking) {
      _gen++;
      await player.stop();
      return _afterTurn();
    }
    if (awake) return goToSleep();
    await mic.stop();
    return wakeUp();
  }

  // --- listening --------------------------------------------------------------
  Future<void> listen() async {
    if (!awake || paused) return;
    _setMode(OrbMode.listening);
    mic.asleep = false;
    mic.onUtterance = _onUtterance;
    if (!await mic.start()) {
      _showError('${Store.instance.wakeWord} needs microphone permission');
      await goToSleep();
    }
  }

  Future<void> _onUtterance(Uint8List wav) async {
    if (!awake || mode != OrbMode.listening) return;
    final gen = ++_gen;
    _setMode(OrbMode.thinking);
    await mic.stop(); // no echo, and replies play on the loudspeaker
    try {
      final text = await deepgram.transcribe(wav);
      if (gen != _gen) return;
      if (text.isEmpty) return listen();
      if (isSleepCommand(text)) {
        you = text;
        reply = '';
        return goToSleep();
      }
      await ask(text);
    } catch (e) {
      if (gen != _gen) return;
      _showError('$e');
      await listen();
    }
  }

  // --- a conversation turn ----------------------------------------------------
  final _clips = <Future<Object?>>[];
  int _released = 0;
  bool _release = false, _muted = false, _filled = false;
  Future<void> _pump = Future.value();

  void _queueSpeech(String sentence, [Future<Uint8List> Function(String)? synth]) {
    if (_muted || !Store.instance.speakReplies || !RegExp(r'[\w\u0900-\u097F\u0C00-\u0C7F]').hasMatch(sentence)) return;
    final text = sentence.trim();
    final lang = Deepgram.indicLanguage(text);
    if (lang.isNotEmpty && cfg.sarvamKey.isEmpty) {
      _clips.add(Future<Object?>.value(SayText(Deepgram.cleanForSpeech(text), lang)));
    } else {
      _clips.add((synth ?? deepgram.speak)(text).then<Object?>((b) => b).catchError((_) => null));
    }
    _drain();
  }

  void _drain() {
    if (!_release) return;
    final gen = _gen;
    while (_released < _clips.length) {
      final clip = _clips[_released++];
      _pump = _pump.then((_) async {
        final item = await clip;
        if (item != null && gen == _gen && !_muted) player.add(item);
      });
    }
  }

  static const _fillers = ['One sec, let me check.', 'Let me look that up.', 'Give me a second.'];
  final _fillerCache = <String, Uint8List>{};

  Future<Uint8List> _filler(String text) async => _fillerCache[text] ??= await deepgram.speak(text);

  Future<void> ask(String text) async {
    final gen = ++_gen;
    you = text;
    reply = '';
    detail = '';
    steps.clear();
    _clips.clear();
    _released = 0;
    _release = false;
    _muted = false;
    _filled = false;
    _pump = Future.value();
    _setMode(OrbMode.thinking);
    await mic.stop();

    var full = '', spokenLen = 0, buffer = '';
    final sentenceEnd = RegExp(r'(?<=[.!?।…])["\x27)\]]*\s+');

    void feed(String more) {
      buffer += more;
      while (true) {
        final m = sentenceEnd.allMatches(buffer).where((m) => m.start >= 24).firstOrNull;
        if (m != null) {
          _queueSpeech(buffer.substring(0, m.end));
          buffer = buffer.substring(m.end);
          continue;
        }
        if (buffer.length > 260) {
          final cut = max(buffer.lastIndexOf(', ', 240), buffer.lastIndexOf(' ', 240));
          if (cut > 40) {
            _queueSpeech(buffer.substring(0, cut + 1));
            buffer = buffer.substring(cut + 1);
            continue;
          }
        }
        break;
      }
    }

    try {
      await for (final event in brain.ask(text)) {
        if (gen != _gen) return; // interrupted: leaving the loop cancels the stream
        switch (event) {
          case TextDelta(:final text):
            full += text;
            final (visible, hasCode) = visibleText(full, isFinal: false);
            if (hasCode && !_muted) {
              _muted = true;
              detail = 'writing code';
            }
            if (visible.length > spokenLen) {
              feed(visible.substring(spokenLen));
              spokenLen = visible.length;
              reply = visible.replaceAll(RegExp(r'\s+'), ' ').trim();
            }
            if (!hasCode && (visible.length > 160 || RegExp(r'[.!?]\s+[^\s`]').hasMatch(visible))) {
              _release = true;
              _drain();
            }
            notifyListeners();
          case ToolStarted(:final name, :final args):
            detail = toolStatus(name, args);
            if (!_filled && _clips.isEmpty && buffer.trim().isEmpty && Store.instance.language == 'english') {
              final filler = switch (name) {
                'web_search' || 'get_news' => _fillers[Random().nextInt(_fillers.length)],
                'speed_test' => 'Running a speed test, this takes about 15 seconds.',
                'run_command' => 'On it.',
                _ => null,
              };
              if (filler != null) {
                _filled = true;
                _queueSpeech(filler, _filler);
              }
            }
            if (!_muted) {
              _release = true;
              _drain();
            }
            notifyListeners();
          case StepDone(:final text, :final outcome):
            steps.add((text, outcome));
            if (steps.length > 6) steps.removeAt(0);
            notifyListeners();
          case StatusUpdate(:final text):
            detail = text;
            notifyListeners();
        }
      }
      if (gen != _gen) return;

      final (spoken, blocks) = splitCode(full);
      if (blocks.isNotEmpty) {
        _muted = true;
        code = blocks;
        _codeShown.add(blocks);
      } else {
        final (visible, _) = visibleText(full, isFinal: true);
        if (visible.length > spokenLen) feed(visible.substring(spokenLen));
        _queueSpeech(buffer);
        buffer = '';
        _release = true;
        _drain();
      }
      reply = spoken.isNotEmpty ? spoken : (blocks.isNotEmpty ? "Here's the code." : reply);
      notifyListeners();

      await _pump;
      await player.finished();
      if (gen != _gen) return;
      await _afterTurn();
    } catch (e) {
      if (gen != _gen) return;
      _showError('$e');
      await _afterTurn();
    }
  }

  // --- app lifecycle ------------------------------------------------------------
  Future<void> pause() async {
    if (Store.instance.backgroundListening) return; // the foreground service keeps the mic alive
    paused = true;
    _gen++;
    await player.stop();
    await mic.stop();
    passive = false;
    notifyListeners();
  }

  Future<void> resume() async {
    if (!paused) return;
    paused = false;
    await _afterTurn();
  }

  void forgetConversation() {
    brain.reset();
    you = '';
    reply = '';
    notifyListeners();
  }

  // --- code blocks ----------------------------------------------------------------
  static final _fence = RegExp(r'```([^\n`]*)\n(.*?)```', dotAll: true);

  static (String, bool) visibleText(String full, {required bool isFinal}) {
    var out = full.replaceAll(_fence, ' ');
    var hasCode = out != full;
    final open = out.indexOf('```');
    if (open >= 0) {
      out = out.substring(0, open);
      hasCode = true;
    } else if (!isFinal) {
      final trimmed = out.replaceFirst(RegExp(r'`{1,2}$'), '');
      out = trimmed;
    }
    return (out, hasCode);
  }

  static (String, List<CodeBlock>) splitCode(String text) {
    CodeBlock block(String info, String body) {
      final parts = info.replaceAll('title=', ' ').replaceAll(':', ' ').trim().split(RegExp(r'\s+'));
      final lang = parts.isNotEmpty && !parts.first.contains('.') ? parts.first.toLowerCase() : '';
      final filename = parts.where((p) => p.contains('.')).firstOrNull;
      return CodeBlock(lang, filename, body.trimRight());
    }

    final blocks = [for (final m in _fence.allMatches(text)) block(m.group(1)!, m.group(2)!)];
    var spoken = text.replaceAll(_fence, ' ');
    final open = spoken.indexOf('```');
    if (open >= 0) {
      final tail = spoken.substring(open + 3);
      final nl = tail.indexOf('\n');
      blocks.add(block(nl >= 0 ? tail.substring(0, nl) : tail, nl >= 0 ? tail.substring(nl + 1) : ''));
      spoken = spoken.substring(0, open);
    }
    return (spoken.replaceAll(RegExp(r'\s+'), ' ').trim(), blocks.where((b) => b.code.trim().isNotEmpty).toList());
  }

  @override
  void dispose() {
    mic.stop();
    player.stop();
    _codeShown.close();
    super.dispose();
  }
}
