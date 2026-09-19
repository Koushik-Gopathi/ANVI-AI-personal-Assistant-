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
  static const languages = {'english': 'English', 'telugu': 'Telugu', 'hindi': 'Hindi', 'auto': 'Same as I speak'};
  static const languageCodes = {'english': 'en', 'telugu': 'te', 'hindi': 'hi', 'auto': ''};

  // --- memory & history ----------------------------------------------------------
  final memories = <Map<String, dynamic>>[]; // {fact, added}
  final history = <Map<String, dynamic>>[]; // {at, you, karen, steps}

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
      memories.addAll((data['memories'] as List? ?? []).map((m) => Map<String, dynamic>.from(m)));
      history.addAll((data['history'] as List? ?? []).map((m) => Map<String, dynamic>.from(m)));
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
      },
      'memories': memories,
      'history': history.length > 300 ? history.sublist(history.length - 300) : history,
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
