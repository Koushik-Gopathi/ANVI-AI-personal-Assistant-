import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// Settings for the phone app. Filled in by scanning the setup QR code that the
/// PC shows (📱 button → "Android app"), or typed in by hand.
class AnviConfig {
  String groqKey = '';
  String deepgramKey = '';
  String tavilyKey = '';

  /// Pairing secret + addresses of Karen on the PC, used for PC actions.
  String pairToken = '';
  String lanUrl = '';
  String publicUrl = '';

  String groqModel = 'openai/gpt-oss-120b';
  String fallbackModel = 'openai/gpt-oss-20b';
  String sttModel = 'nova-3';
  String sttLanguage = 'en-IN';
  String voice = 'aura-asteria-en';

  bool get ready => groqKey.isNotEmpty && deepgramKey.isNotEmpty;
  bool get hasPc => pairToken.isNotEmpty && (lanUrl.isNotEmpty || publicUrl.isNotEmpty);

  static const _fields = ['groqKey', 'deepgramKey', 'tavilyKey', 'pairToken', 'lanUrl', 'publicUrl'];

  static Future<AnviConfig> load() async {
    final prefs = await SharedPreferences.getInstance();
    final c = AnviConfig();
    c._set(Map.fromEntries(_fields.map((f) => MapEntry(f, prefs.getString(f) ?? ''))));
    return c;
  }

  Future<void> save() async {
    final prefs = await SharedPreferences.getInstance();
    final values = _values();
    for (final f in _fields) {
      await prefs.setString(f, values[f]!);
    }
  }

  /// Returns false if [raw] isn't a Karen setup code.
  bool applySetupCode(String raw) {
    try {
      final data = jsonDecode(raw);
      if (data is! Map || data['anvi'] != 1) return false;
      groqKey = '${data['groq'] ?? ''}'.trim();
      deepgramKey = '${data['deepgram'] ?? ''}'.trim();
      tavilyKey = '${data['tavily'] ?? ''}'.trim();
      pairToken = '${data['token'] ?? ''}'.trim();
      lanUrl = _trimUrl('${data['lan'] ?? ''}');
      publicUrl = _trimUrl('${data['public'] ?? ''}');
      return ready;
    } catch (_) {
      return false;
    }
  }

  static String _trimUrl(String url) => url.trim().replaceAll(RegExp(r'/+$'), '');

  Map<String, String> _values() => {
        'groqKey': groqKey,
        'deepgramKey': deepgramKey,
        'tavilyKey': tavilyKey,
        'pairToken': pairToken,
        'lanUrl': lanUrl,
        'publicUrl': publicUrl,
      };

  void _set(Map<String, String> v) {
    groqKey = v['groqKey']!;
    deepgramKey = v['deepgramKey']!;
    tavilyKey = v['tavilyKey']!;
    pairToken = v['pairToken']!;
    lanUrl = v['lanUrl']!;
    publicUrl = v['publicUrl']!;
  }
}
