import 'dart:convert';
import 'dart:io';

import 'package:path_provider/path_provider.dart';

/// Karen's saved state on the phone: settings, long-term memory and chat history
/// (one JSON file in the app's private storage).
class Store {
  Store._();
  static final Store instance = Store._();

  // --- settings ----------------------------------------------------------------
  String wakeWord = 'Karen';
  String voice = 'aura-asteria-en';
  String language = 'english'; // english | telugu | hindi | auto
  bool speakReplies = true;
  bool backgroundListening = true; // keeps listening in Recents / after swiping the app away
  bool bargeIn = true; // talking while Karen speaks interrupts her
  bool sounds = true;
  String brain = 'groq'; // groq | openrouter_free | openrouter

  static const voices = {
    'aura-asteria-en': 'Asteria (US, female)',
    'aura-luna-en': 'Luna (US, female)',
    'aura-stella-en': 'Stella (US, female)',
    'aura-athena-en': 'Athena (UK, female)',
    'aura-hera-en': 'Hera (US, female)',
    'aura-orion-en': 'Orion (US, male)',
    'aura-arcas-en': 'Arcas (US, male)',
    'aura-perseus-en': 'Perseus (US, male)',
    'aura-angus-en': 'Angus (Irish, male)',
    'aura-orpheus-en': 'Orpheus (US, male)',
    'aura-helios-en': 'Helios (UK, male)',
    'aura-zeus-en': 'Zeus (US, male)',
  };
  static const brains = {'groq': 'Groq (free)', 'openrouter_free': 'OpenRouter (free models)',
      'mix': 'Smart mix (free chat, Haiku for work)', 'openrouter': 'OpenRouter (paid, best)'};
  static const languages = {'english': 'English', 'telugu': 'Telugu', 'hindi': 'Hindi', 'auto': 'Same as I speak'};
  static const languageCodes = {'english': 'en', 'telugu': 'te', 'hindi': 'hi', 'auto': ''};

  // --- memory & history ----------------------------------------------------------
  final memories = <Map<String, dynamic>>[]; // {fact, added}
  final history = <Map<String, dynamic>>[]; // {at, you, karen, steps}
  final protocols = <String, Map<String, dynamic>>{}; // key -> {name, steps, saved, runs}

  File? _file;

  Future<void> load() async {
    final dir = await getApplicationDocumentsDirectory();
    _file = File('${dir.path}/karen_state.json');
    try {
      final data = jsonDecode(await _file!.readAsString());
      final s = Map<String, dynamic>.from(data['settings'] ?? {});
      wakeWord = s['wake_word'] ?? wakeWord;
      voice = voices.containsKey(s['voice']) ? s['voice'] : voice;
      language = languages.containsKey(s['language']) ? s['language'] : language;
      speakReplies = s['speak_replies'] ?? speakReplies;
      backgroundListening = s['background_listening'] ?? backgroundListening;
      bargeIn = s['barge_in'] ?? bargeIn;
      sounds = s['sounds'] ?? sounds;
      brain = brains.containsKey(s['brain']) ? s['brain'] : brain;
      memories.addAll((data['memories'] as List? ?? []).map((m) => Map<String, dynamic>.from(m)));
      history.addAll((data['history'] as List? ?? []).map((m) => Map<String, dynamic>.from(m)));
      (data['protocols'] as Map? ?? {}).forEach((k, v) => protocols['$k'] = Map<String, dynamic>.from(v as Map));
    } catch (_) {
      // first run or unreadable file: keep defaults
    }
  }

  Future<void> save() async {
    final file = _file;
    if (file == null) return;
    final tmp = File('${file.path}.tmp');
    await tmp.writeAsString(jsonEncode({
      'settings': {
        'wake_word': wakeWord,
        'voice': voice,
        'language': language,
        'speak_replies': speakReplies,
        'background_listening': backgroundListening,
        'barge_in': bargeIn,
        'sounds': sounds,
        'brain': brain,
      },
      'memories': memories,
      'history': history.length > 300 ? history.sublist(history.length - 300) : history,
      'protocols': protocols,
    }));
    await tmp.rename(file.path);
  }

  String? setWakeWord(String word) {
    final clean = word.replaceAll(RegExp(r'[^A-Za-z ]'), '').trim();
    if (clean.length < 2 || clean.length > 20) return 'wake word must be 2-20 letters';
    wakeWord = clean[0].toUpperCase() + clean.substring(1);
    return null;
  }

  // --- memory ----------------------------------------------------------------------
  Map<String, dynamic> remember(String fact) {
    final f = fact.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (f.isEmpty) return {'error': 'nothing to remember'};
    if (memories.any((m) => '${m['fact']}'.toLowerCase() == f.toLowerCase())) return {'already_remembered': f};
    final now = DateTime.now();
    memories.add({'fact': f, 'added': '${now.day}/${now.month}/${now.year}'});
    if (memories.length > 200) memories.removeAt(0);
    save();
    return {'remembered': f};
  }

  Map<String, dynamic> forget(String fact) {
    final words = RegExp(r'\w+').allMatches(fact.toLowerCase()).map((m) => m.group(0)!).where((w) => w.length > 2);
    final removed = memories
        .where((m) => words.isNotEmpty && words.every('${m['fact']}'.toLowerCase().contains))
        .map((m) => '${m['fact']}')
        .toList();
    if (removed.isEmpty) return {'error': "nothing remembered matches '$fact'"};
    memories.removeWhere((m) => removed.contains('${m['fact']}'));
    save();
    return {'forgot': removed};
  }

  // --- protocols: named routines ("study mode") started by saying the name ---------------
  static String _protocolKey(String name) =>
      name.toLowerCase().replaceAll(RegExp(r'[^a-z0-9 ]'), ' ').replaceAll(RegExp(r'\s+'), ' ').trim();

  Map<String, dynamic> saveProtocol(String name, Object? rawSteps) {
    final key = _protocolKey(name);
    if (key.length < 2 || key.length > 40) return {'error': "a protocol name must be 2-40 letters, like 'study mode'"};
    final parts = rawSteps is List
        ? rawSteps.map((x) => '$x')
        : '${rawSteps ?? ''}'.split(RegExp(r'\s*(?:\n|;|\bthen\b)\s*'));
    final steps = parts
        .map((x) => x.replaceAll(RegExp(r'\s+'), ' ').trim())
        .where((x) => x.isNotEmpty)
        .map((x) => x.length > 200 ? x.substring(0, 200) : x)
        .take(15)
        .toList();
    if (steps.isEmpty) return {'error': 'a protocol needs at least one step'};
    final replaced = protocols.containsKey(key);
    final now = DateTime.now();
    protocols[key] = {
      'name': name.trim(),
      'steps': steps,
      'saved': '${now.day}/${now.month}/${now.year}',
      'runs': protocols[key]?['runs'] ?? 0,
    };
    save();
    return {'saved': name.trim(), 'steps': steps, 'replaced': replaced, 'how_to_use': "say '${name.trim()}' to run it"};
  }

  Map<String, dynamic> deleteProtocol(String name) {
    final removed = protocols.remove(_protocolKey(name));
    if (removed == null) {
      return {'error': "no protocol called '$name'", 'saved_protocols': [for (final p in protocols.values) p['name']]};
    }
    save();
    return {'deleted': removed['name']};
  }

  Map<String, dynamic>? protocol(String name) => protocols[_protocolKey(name)];

  void markProtocolRun(String name) {
    final p = protocol(name);
    if (p == null) return;
    p['runs'] = (p['runs'] ?? 0) + 1;
    save();
  }

  /// The protocol a request names ("Karen, study mode please" -> study mode), if any.
  Map<String, dynamic>? protocolIn(String text) {
    final words = ' ${_protocolKey(text)} ';
    if (RegExp(r'\b(save|create|make|add|delete|remove|forget|edit|change|list|what|show)\b').hasMatch(words)) {
      return null; // talking about protocols, not starting one
    }
    Map<String, dynamic>? best;
    for (final p in protocols.values) {
      final key = _protocolKey('${p['name']}');
      if (words.contains(' $key ') && (best == null || key.length > _protocolKey('${best['name']}').length)) best = p;
    }
    return best;
  }

  String protocolPrompt() {
    if (protocols.isEmpty) return '';
    final names = protocols.values.map((p) => "'${p['name']}' (${(p['steps'] as List).length} steps)").join(', ');
    return 'Saved protocols the user triggers by saying their name: $names. When the user says one, call run_protocol '
        'and then carry out every step. ';
  }

  String memoryPrompt() {
    if (memories.isEmpty) return '';
    final recent = memories.length > 40 ? memories.sublist(memories.length - 40) : memories;
    return 'Things the user asked you to remember: ${recent.map((m) => '${m['fact']} (saved ${m['added']})').join('; ')}. ';
  }

  // --- history -----------------------------------------------------------------------
  void logExchange(String you, String karen, List<(String, String)> steps) {
    history.add({
      'at': DateTime.now().millisecondsSinceEpoch,
      'you': you,
      'karen': karen,
      'steps': [for (final (text, outcome) in steps) '$text${outcome == 'done' ? '' : ' ($outcome)'}'],
    });
    save();
  }

  void clearHistory() {
    history.clear();
    save();
  }
}
