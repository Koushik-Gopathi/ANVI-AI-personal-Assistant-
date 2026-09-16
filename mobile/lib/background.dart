import 'package:flutter_foreground_task/flutter_foreground_task.dart';

/// Keeps Karen's microphone alive while the app is in Recents or the screen is off,
/// using an Android foreground service (shows a "Karen is listening" notification).
class BackgroundListening {
  static bool _initialized = false;

  static void _init() {
    if (_initialized) return;
    _initialized = true;
    FlutterForegroundTask.init(
      androidNotificationOptions: AndroidNotificationOptions(
        channelId: 'karen_listening',
        channelName: 'Karen listening',
        channelDescription: 'Shown while Karen listens for her name in the background',
      ),
      iosNotificationOptions: const IOSNotificationOptions(showNotification: false),
      foregroundTaskOptions: ForegroundTaskOptions(
        eventAction: ForegroundTaskEventAction.nothing(),
        allowWakeLock: true,
        allowWifiLock: true,
      ),
    );
  }

  /// Must be called while the app is on screen (Android only allows starting a
  /// microphone service from the foreground).
  static Future<bool> start(String name) async {
    _init();
    if (await FlutterForegroundTask.checkNotificationPermission() != NotificationPermission.granted) {
      await FlutterForegroundTask.requestNotificationPermission();
    }
    if (await FlutterForegroundTask.isRunningService) return true;
    final result = await FlutterForegroundTask.startService(
      serviceId: 7,
      serviceTypes: [ForegroundServiceTypes.microphone],
      notificationTitle: '$name is listening',
      notificationText: 'Say "$name" any time. Turn this off in settings.',
      notificationInitialRoute: '/',
    );
    return result is ServiceRequestSuccess;
  }

  static Future<void> stop() async {
    _init();
    if (await FlutterForegroundTask.isRunningService) await FlutterForegroundTask.stopService();
  }

  static Future<bool> get batteryUnrestricted => FlutterForegroundTask.isIgnoringBatteryOptimizations;
  static Future<bool> askBatteryUnrestricted() => FlutterForegroundTask.requestIgnoreBatteryOptimization();
}
