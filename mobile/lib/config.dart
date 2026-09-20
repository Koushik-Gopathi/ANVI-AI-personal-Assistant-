import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import 'store.dart';

/// Settings for the phone app. Filled in by scanning the setup QR code that the
/// PC shows (📱 button → "Android app"), or typed in by hand.
class AnviConfig {
  String groqKey = '';
  String deepgramKey = '';
  String tavilyKey = '';
  String sarvamKey = ''; // Telugu/Hindi voice (optional)
  String openrouterKey = ''; // pay-as-you-go brain (optional)

  /// Pairing secret + addresses of Karen on the PC, used for PC actions.
  String pairToken = '';
  String lanUrl = '';
  String publicUrl = '';

  String groqModel = 'openai/gpt-oss-120b';
  String fallbackModel = 'openai/gpt-oss-20b';

  /// The brains Karen can think with. Groq is free but limited; OpenRouter needs the key from the
  /// setup code, and its paid models are much better at long multi-step jobs.
  static const brains = {
    'groq': {
      'label': 'Groq (free)',
      'url': 'https://api.groq.com/openai/v1/chat/completions',
      'models': ['openai/gpt-oss-120b', 'openai/gpt-oss-20b'],
      'vision': 'qwen/qwen3.8-27b',
    },
    'openrouter_free': {
      'label': 'OpenRouter (free models)',
      'url': 'https://openrouter.ai/api/v1/chat/completions',
      'models': ['qwen/qwen3.8-27b:free', 'google/gemma-4-26b-a4b-it:free'],
      'vision': 'qwen/qwen3.8-27b:free',
    },
    'mix': {
      'label': 'Smart mix (free chat, Haiku for work)',
      'url': 'https://openrouter.ai/api/v1/chat/completions',
      'models': <String>[], // chosen per request by turnModels()
      'vision': 'qwen/qwen3.8-27b',
    },
    'openrouter': {
      'label': 'OpenRouter (paid, best)',
      'url': 'https://openrouter.ai/api/v1/chat/completions',
      'models': ['anthropic/claude-haiku-4.5', 'google/gemini-3.1-flash-lite'],
      'vision': 'qwen/qwen3.8-27b:free',
    },
  };

  /// The chosen brain, falling back to Groq when OpenRouter has no key.
  String get brainName {
    final chosen = Store.instance.brain;
    if (chosen != 'groq' && openrouterKey.isEmpty) return 'groq';
    return brains.containsKey(chosen) ? chosen : 'groq';
  }

  Map<String, dynamic> get brain => brains[brainName]!;

  /// (provider, model) to try for this turn, best first. The smart mix keeps free Groq for chat and
  /// pays for Claude only when the user asked for something to be done; each is the other's fallback.
  List<(String, String)> turnModels(bool wantsAction) {
    final name = brainName;
    if (name != 'mix') {
      return [for (final m in (brain['models'] as List).cast<String>()) (name, m)];
    }
    final free = [for (final m in (brains['groq']!['models'] as List).cast<String>()) ('groq', m)];
    final paid = [for (final m in (brains['openrouter']!['models'] as List).cast<String>()) ('openrouter', m)];
    return wantsAction ? [...paid, ...free] : [...free, ...paid];
  }

  String urlFor(String provider) => '${brains[provider]!['url']}';
  String keyFor(String provider) => provider == 'groq' ? groqKey : openrouterKey;
  List<String> get brainModels => [for (final (_, m) in turnModels(true)) m];
  String get brainUrl => urlFor(brainName == 'mix' ? 'groq' : brainName);
  String get brainKey => keyFor(brainName == 'mix' ? 'groq' : brainName);
  String get visionModel => '${brain['vision']}';
  String sttModel = 'nova-3';
  String sttLanguage = 'en-IN';
  String voice = 'aura-asteria-en';

  bool get ready => groqKey.isNotEmpty && deepgramKey.isNotEmpty;
  bool get hasPc => pairToken.isNotEmpty && (lanUrl.isNotEmpty || publicUrl.isNotEmpty);

  static const _fields = ['groqKey', 'deepgramKey', 'tavilyKey', 'sarvamKey', 'openrouterKey', 'pairToken', 'lanUrl',
      'publicUrl'];

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
      sarvamKey = '${data['sarvam'] ?? ''}'.trim();
      openrouterKey = '${data['openrouter'] ?? ''}'.trim();
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
        'sarvamKey': sarvamKey,
        'openrouterKey': openrouterKey,
        'pairToken': pairToken,
        'lanUrl': lanUrl,
        'publicUrl': publicUrl,
      };

  void _set(Map<String, String> v) {
    groqKey = v['groqKey']!;
    deepgramKey = v['deepgramKey']!;
    tavilyKey = v['tavilyKey']!;
    sarvamKey = v['sarvamKey']!;
    openrouterKey = v['openrouterKey']!;
    pairToken = v['pairToken']!;
    lanUrl = v['lanUrl']!;
    publicUrl = v['publicUrl']!;
  }
}
