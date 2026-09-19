import 'dart:async';
import 'dart:io' show SocketException;
import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:http/http.dart' as http;
import 'package:record/record.dart';

import 'config.dart';
import 'diag.dart';
import 'store.dart';

class AnviError implements Exception {
  final String message;
  AnviError(this.message);
  @override
  String toString() => message;
}

// ---------------------------------------------------------------------------
// Deepgram speech-to-text and text-to-speech
// ---------------------------------------------------------------------------
class Deepgram {
  final AnviConfig cfg;
  final http.Client client;
  Deepgram(this.cfg, this.client);

  /// Retry a request a couple of times when the connection is slow or drops.
  static Future<http.Response> _withRetry(Future<http.Response> Function() send) async {
    for (var attempt = 1;; attempt++) {
      try {
        return await send();
      } on Exception catch (e) {
        final network = e is TimeoutException || e is SocketException || e is http.ClientException;
        if (!network || attempt >= 3) {
          if (network) throw AnviError('The internet is slow right now. Please try again.');
          rethrow;
        }
        await Future.delayed(Duration(milliseconds: 800 * attempt));
      }
    }
  }

  /// English (and the wake word) via Deepgram; Telugu/Hindi/any language via Groq Whisper.
  Future<String> transcribe(Uint8List wav, {bool wake = false}) async {
    final watch = Stopwatch()..start();
    final text = await _transcribe(wav, wake: wake);
    if (wake) {
      if (text.isNotEmpty) Diag.wakeHeard = text;
    } else {
      Diag.sttMs = watch.elapsedMilliseconds;
      Diag.heard = text.isEmpty ? '(nothing — noise?)' : text;
      Diag.heardAt = DateTime.now();
    }
    return text;
  }

  Future<String> _transcribe(Uint8List wav, {bool wake = false}) async {
    final store = Store.instance;
    if (!wake && store.language != 'english') {
      final r = await _withRetry(() async {
        final req = http.MultipartRequest('POST', Uri.parse('https://api.groq.com/openai/v1/audio/transcriptions'))
          ..headers['Authorization'] = 'Bearer ${cfg.groqKey}'
          ..fields['model'] = 'whisper-large-v3'
          ..fields['response_format'] = 'json'
          ..fields['prompt'] = store.wakeWord
          ..files.add(http.MultipartFile.fromBytes('file', wav, filename: 'speech.wav'));
        final code = Store.languageCodes[store.language] ?? '';
        if (code.isNotEmpty) req.fields['language'] = code;
        return http.Response.fromStream(await client.send(req).timeout(const Duration(seconds: 45)));
      });
      if (r.statusCode != 200) throw AnviError('speech-to-text failed (${r.statusCode})');
      return '${jsonDecode(utf8.decode(r.bodyBytes))['text'] ?? ''}'.trim();
    }
    final params = {
      'model': cfg.sttModel,
      'language': cfg.sttLanguage,
      'smart_format': 'true',
      'punctuate': 'true',
      if (cfg.sttModel.startsWith('nova-3')) 'keyterm': store.wakeWord,
    };
    final r = await _withRetry(() => client
        .post(Uri.https('api.deepgram.com', '/v1/listen', params),
            headers: {'Authorization': 'Token ${cfg.deepgramKey}', 'Content-Type': 'audio/wav'}, body: wav)
        .timeout(const Duration(seconds: 30)));
    if (r.statusCode != 200) throw AnviError('speech-to-text failed (${r.statusCode})');
    final data = jsonDecode(utf8.decode(r.bodyBytes));
    return '${data['results']?['channels']?[0]?['alternatives']?[0]?['transcript'] ?? ''}'.trim();
  }

  /// Telugu or Hindi script in [text] -> 'te-IN' / 'hi-IN', else ''.
  static String indicLanguage(String text) {
    if (RegExp(r'[\u0C00-\u0C7F]').hasMatch(text)) return 'te-IN';
    if (RegExp(r'[\u0900-\u097F]').hasMatch(text)) return 'hi-IN';
    return '';
  }

  /// MP3 bytes for [text]; Telugu/Hindi use Sarvam AI when a key is set (see [SayText] otherwise).
  Future<Uint8List> speak(String text) async {
    final lang = indicLanguage(text);
    if (lang.isNotEmpty) {
      final r = await _withRetry(() => client
          .post(Uri.parse('https://api.sarvam.ai/text-to-speech'),
              headers: {'api-subscription-key': cfg.sarvamKey, 'Content-Type': 'application/json'},
              body: jsonEncode({
                'text': cleanForSpeech(text),
                'language_code': lang,
                'model': 'bulbul:v3',
                'speaker': 'priya',
                'output_audio_codec': 'mp3',
              }))
          .timeout(const Duration(seconds: 30)));
      if (r.statusCode != 200) throw AnviError('Telugu/Hindi voice failed (${r.statusCode})');
      return base64Decode((jsonDecode(r.body)['audios'] as List).first as String);
    }
    final r = await _withRetry(() => client
        .post(Uri.https('api.deepgram.com', '/v1/speak', {'model': Store.instance.voice}),
            headers: {'Authorization': 'Token ${cfg.deepgramKey}', 'Content-Type': 'application/json'},
            body: jsonEncode({'text': cleanForSpeech(text)}))
        .timeout(const Duration(seconds: 30)));
    if (r.statusCode != 200) throw AnviError('voice failed (${r.statusCode})');
    return r.bodyBytes;
  }

  static String cleanForSpeech(String text) {
    var t = text.replaceAll(' ', ' ').replaceAll(' ', ' ');
    t = t.replaceAll(RegExp(r'\*\*|__|`|#+\s?'), '');
    t = t.replaceAll(RegExp(r'https?://\S+'), '');
    t = t.replaceAll(RegExp(r'^\s*[-*]\s+', multiLine: true), '');
    final (degrees, rupees) = switch (indicLanguage(t)) {
      'te-IN' => (' డిగ్రీలు', 'రూపాయలు '),
      'hi-IN' => (' डिग्री', 'रुपये '),
      _ => (' degrees', 'rupees '),
    };
    t = t.replaceAll('°C', degrees).replaceAll('°F', '$degrees Fahrenheit').replaceAll('°', degrees);
    t = t.replaceAll(RegExp(r'₹\s?'), rupees);
    t = t.replaceAll(RegExp(r'\s+'), ' ').trim();
    return t.length > 1900 ? t.substring(0, 1900) : t;
  }
}

// ---------------------------------------------------------------------------
// Microphone with voice-activity detection
// ---------------------------------------------------------------------------
/// Streams 16 kHz mono PCM from the mic and calls [onUtterance] with a WAV
/// file each time someone finishes speaking.
class MicListener {
  static const _rate = 16000;
  final _recorder = AudioRecorder();
  StreamSubscription<Uint8List>? _sub;

  /// Asleep: only short phrases matter ("Karen ..."), so takes are cut short.
  bool asleep = false;

  /// Karen is talking: only clearly louder, longer speech counts (she may hear herself).
  bool strict = false;
  void Function(Uint8List wav)? onUtterance;
  void Function()? onSpeechStart;
  void Function()? onFalseStart;
  double level = 0;

  final _chunks = <Uint8List>[];
  int _bytes = 0;
  double _floor = 0.004;
  int _voicedMs = 0, _silenceMs = 0, _totalVoicedMs = 0, _speechMs = 0;
  bool _speaking = false;

  bool get running => _sub != null;

  Future<bool> start() async {
    if (_sub != null) return true;
    if (!await _recorder.hasPermission()) return false;
    final stream = await _recorder.startStream(const RecordConfig(
      encoder: AudioEncoder.pcm16bits,
      sampleRate: _rate,
      numChannels: 1,
      echoCancel: true,
      noiseSuppress: true,
      autoGain: true,
      // voiceRecognition keeps the phone in normal audio mode, so replies play on the loudspeaker
      androidConfig: AndroidRecordConfig(audioSource: AndroidAudioSource.voiceRecognition),
    ));
    _reset();
    _sub = stream.listen(_onChunk);
    return true;
  }

  Future<void> stop() async {
    final sub = _sub;
    _sub = null;
    await sub?.cancel();
    if (await _recorder.isRecording()) await _recorder.stop();
    _reset();
    level = 0;
  }

  void _reset() {
    _chunks.clear();
    _bytes = 0;
    _voicedMs = _silenceMs = _totalVoicedMs = _speechMs = 0;
    _speaking = false;
  }

  void _onChunk(Uint8List chunk) {
    final samples = chunk.length ~/ 2;
    if (samples == 0) return;
    final data = ByteData.sublistView(chunk);
    var sum = 0.0;
    for (var i = 0; i < samples; i++) {
      final s = data.getInt16(i * 2, Endian.little) / 32768.0;
      sum += s * s;
    }
    final rms = sqrt(sum / samples);
    level = rms;
    final dt = samples * 1000 ~/ _rate;

    _chunks.add(chunk);
    _bytes += chunk.length;
    if (!_speaking) {
      // keep ~0.5 s of lead-in so the first syllable isn't cut off
      while (_chunks.length > 1 && _bytes - _chunks.first.length > _rate) {
        _bytes -= _chunks.removeAt(0).length;
      }
      _floor = _floor * 0.97 + min(rms, _floor * 3) * 0.03;
    }

    final threshold = strict ? max(0.03, _floor * 4) : max(0.012, _floor * 3);
    if (rms > threshold) {
      _voicedMs += dt;
      _silenceMs = 0;
      if (_speaking) _totalVoicedMs += dt;
      if (!_speaking && _voicedMs >= (strict ? 280 : 160)) {
        onSpeechStart?.call();
        _speaking = true;
        _totalVoicedMs = _voicedMs;
      }
    } else {
      _voicedMs = max(0, _voicedMs - dt ~/ 2);
      if (_speaking) _silenceMs += dt;
    }
    if (_speaking) _speechMs += dt;

    final endSilence = asleep ? 600 : (strict ? 700 : 900);
    final maxLength = asleep ? 4500 : (strict ? 10000 : 25000);
    if (_speaking && (_silenceMs >= endSilence || _speechMs > maxLength)) {
      final tooShort = _totalVoicedMs < (strict ? 350 : 250);
      final tooLong = asleep && _speechMs > maxLength;
      final pcm = Uint8List(_bytes);
      var offset = 0;
      for (final c in _chunks) {
        pcm.setAll(offset, c);
        offset += c.length;
      }
      _reset();
      if (!tooShort && !tooLong) {
        onUtterance?.call(_wav(pcm));
      } else {
        onFalseStart?.call();
      }
    }
  }

  static Uint8List _wav(Uint8List pcm) {
    final header = ByteData(44);
    void ascii(int at, String s) {
      for (var i = 0; i < s.length; i++) {
        header.setUint8(at + i, s.codeUnitAt(i));
      }
    }

    ascii(0, 'RIFF');
    header.setUint32(4, 36 + pcm.length, Endian.little);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    header.setUint32(16, 16, Endian.little);
    header.setUint16(20, 1, Endian.little);
    header.setUint16(22, 1, Endian.little);
    header.setUint32(24, _rate, Endian.little);
    header.setUint32(28, _rate * 2, Endian.little);
    header.setUint16(32, 2, Endian.little);
    header.setUint16(34, 16, Endian.little);
    ascii(36, 'data');
    header.setUint32(40, pcm.length, Endian.little);
    return Uint8List.fromList([...header.buffer.asUint8List(), ...pcm]);
  }
}

// ---------------------------------------------------------------------------
// Speaker: plays reply clips back to back on the loudspeaker
// ---------------------------------------------------------------------------
/// Text for the phone's own text-to-speech voice (Telugu/Hindi without a Sarvam key).
class SayText {
  final String text;
  final String lang;
  SayText(this.text, this.lang);
}

class SpeechPlayer {
  final _player = AudioPlayer();
  final _tts = FlutterTts();
  final _queue = <Object>[]; // Uint8List (MP3) or SayText
  bool playing = false;
  Completer<void>? _idle;
  void Function()? onStart;
  void Function(String)? onProblem;

  SpeechPlayer() {
    _player.setAudioContext(AudioContextConfig(
      route: AudioContextConfigRoute.speaker,
      focus: AudioContextConfigFocus.gain,
    ).build());
    _player.onPlayerComplete.listen((_) => _next());
    _tts.awaitSpeakCompletion(true);
  }

  /// Quieter while checking whether the user is talking over Karen.
  void duck(bool on) => _player.setVolume(on ? 0.3 : 1.0).catchError((_) {});

  void add(Object clip) {
    _queue.add(clip);
    if (!playing) _next();
  }

  Future<void> _next() async {
    if (_queue.isEmpty) {
      playing = false;
      _idle?.complete();
      _idle = null;
      return;
    }
    playing = true;
    onStart?.call();
    final item = _queue.removeAt(0);
    if (item is SayText) {
      try {
        final available = await _tts.isLanguageAvailable(item.lang);
        if (available == true) {
          await _tts.setLanguage(item.lang);
          await _tts.speak(item.text);
        } else {
          onProblem?.call('no ${item.lang.startsWith('te') ? 'Telugu' : 'Hindi'} voice installed on this phone');
        }
      } catch (_) {}
      return _next();
    }
    try {
      await _player.play(BytesSource(item as Uint8List, mimeType: 'audio/mpeg'));
      Diag.clipsPlayed++;
    } catch (_) {
      Diag.clipsFailed++;
      _next();
    }
  }

  /// Completes when everything queued so far has finished playing.
  Future<void> finished() {
    if (!playing && _queue.isEmpty) return Future.value();
    return (_idle ??= Completer<void>()).future;
  }

  Future<void> stop() async {
    _queue.clear();
    await _player.stop();
    duck(false);
    await _tts.stop();
    playing = false;
    _idle?.complete();
    _idle = null;
  }
}
