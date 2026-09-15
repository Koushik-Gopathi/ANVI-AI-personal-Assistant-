import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:http/http.dart' as http;
import 'package:record/record.dart';

import 'config.dart';

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

  Future<String> transcribe(Uint8List wav) async {
    final params = {
      'model': cfg.sttModel,
      'language': cfg.sttLanguage,
      'smart_format': 'true',
      'punctuate': 'true',
      if (cfg.sttModel.startsWith('nova-3')) 'keyterm': 'Karen',
    };
    final r = await client
        .post(Uri.https('api.deepgram.com', '/v1/listen', params),
            headers: {'Authorization': 'Token ${cfg.deepgramKey}', 'Content-Type': 'audio/wav'}, body: wav)
        .timeout(const Duration(seconds: 30));
    if (r.statusCode != 200) throw AnviError('speech-to-text failed (${r.statusCode})');
    final data = jsonDecode(utf8.decode(r.bodyBytes));
    return '${data['results']?['channels']?[0]?['alternatives']?[0]?['transcript'] ?? ''}'.trim();
  }

  Future<Uint8List> speak(String text) async {
    final r = await client
        .post(Uri.https('api.deepgram.com', '/v1/speak', {'model': cfg.voice}),
            headers: {'Authorization': 'Token ${cfg.deepgramKey}', 'Content-Type': 'application/json'},
            body: jsonEncode({'text': cleanForSpeech(text)}))
        .timeout(const Duration(seconds: 30));
    if (r.statusCode != 200) throw AnviError('voice failed (${r.statusCode})');
    return r.bodyBytes;
  }

  static String cleanForSpeech(String text) {
    var t = text.replaceAll(' ', ' ').replaceAll(' ', ' ');
    t = t.replaceAll(RegExp(r'\*\*|__|`|#+\s?'), '');
    t = t.replaceAll(RegExp(r'https?://\S+'), '');
    t = t.replaceAll(RegExp(r'^\s*[-*]\s+', multiLine: true), '');
    t = t.replaceAll('°C', ' degrees').replaceAll('°F', ' degrees Fahrenheit').replaceAll('°', ' degrees');
    t = t.replaceAll(RegExp(r'₹\s?'), 'rupees ');
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
  void Function(Uint8List wav)? onUtterance;
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

    final threshold = max(0.012, _floor * 3);
    if (rms > threshold) {
      _voicedMs += dt;
      _silenceMs = 0;
      if (_speaking) _totalVoicedMs += dt;
      if (!_speaking && _voicedMs >= 160) {
        _speaking = true;
        _totalVoicedMs = _voicedMs;
      }
    } else {
      _voicedMs = max(0, _voicedMs - dt ~/ 2);
      if (_speaking) _silenceMs += dt;
    }
    if (_speaking) _speechMs += dt;

    final endSilence = asleep ? 600 : 900;
    final maxLength = asleep ? 4500 : 25000;
    if (_speaking && (_silenceMs >= endSilence || _speechMs > maxLength)) {
      final tooShort = _totalVoicedMs < 250;
      final tooLong = asleep && _speechMs > maxLength;
      final pcm = Uint8List(_bytes);
      var offset = 0;
      for (final c in _chunks) {
        pcm.setAll(offset, c);
        offset += c.length;
      }
      _reset();
      if (!tooShort && !tooLong) onUtterance?.call(_wav(pcm));
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
class SpeechPlayer {
  final _player = AudioPlayer();
  final _queue = <Uint8List>[];
  bool playing = false;
  Completer<void>? _idle;
  void Function()? onStart;

  SpeechPlayer() {
    _player.setAudioContext(AudioContextConfig(
      route: AudioContextConfigRoute.speaker,
      focus: AudioContextConfigFocus.gain,
    ).build());
    _player.onPlayerComplete.listen((_) => _next());
  }

  void add(Uint8List mp3) {
    _queue.add(mp3);
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
    try {
      await _player.play(BytesSource(_queue.removeAt(0), mimeType: 'audio/mpeg'));
    } catch (_) {
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
    playing = false;
    _idle?.complete();
    _idle = null;
  }
}
