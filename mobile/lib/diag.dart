/// What the voice loop is doing, for the diagnostics screen. Written from everywhere, never read by
/// the loop itself: hearing and speaking both fail silently, so this turns "it doesn't work" into a fact.
class Diag {
  static String heard = '';
  static DateTime? heardAt;
  static int sttMs = 0;
  static String wakeHeard = '';
  static int clipsPlayed = 0;
  static int clipsFailed = 0;
  static String lastError = '';
  static DateTime? lastErrorAt;
  static int bargeIns = 0;
  static int echoesIgnored = 0;

  static String ago(DateTime? t) {
    if (t == null) return 'never';
    final s = DateTime.now().difference(t).inSeconds;
    return s < 60 ? '${s}s ago' : '${s ~/ 60} min ago';
  }
}
