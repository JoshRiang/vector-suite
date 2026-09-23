/// Shared API client for the VECTOR Suite apps.
///
/// All three apps talk to ONE backend. The LLM key lives server-side only --
/// shipping it in an APK would make it extractable with `unzip` + `strings`,
/// so no app ever calls a model directly.
///
/// The base URL is Tailscale-private by default. Override at build time with:
///   flutter build apk --dart-define=API_BASE=http://host:port
library;

import 'dart:convert';
import 'dart:async' show TimeoutException;
import 'dart:io' show HandshakeException, SocketException;

import 'package:http/http.dart' as http;

class Api {
  Api({String? baseUrl, required this.userId, String? apiKey, http.Client? client})
      : baseUrl = baseUrl ?? defaultBaseUrl,
        apiKey = apiKey ?? defaultApiKey,
        _client = client;

  /// Injectable HTTP client, used by tests to exercise the failure paths.
  ///
  /// The exception mapping below is what stands between a network blip and a
  /// blank screen, so it must be testable without a real socket.
  final http.Client? _client;

  http.Client get _http => _client ?? http.Client();

  /// Tailscale address of the home server. Private to the user's own network.
  static const defaultBaseUrl = String.fromEnvironment(
    'API_BASE',
    defaultValue: 'http://100.89.180.23:8790',
  );

  /// Stable owner id.
  ///
  /// This is a single-user personal deployment, so the id is a constant rather
  /// than a random per-install value. That matters: the proactive morning
  /// brief runs server-side and must read the SAME rows the apps write. A
  /// random per-install id would silently split the data in two and the brief
  /// would always report "no goals".
  static const defaultUserId = String.fromEnvironment(
    'USER_ID',
    defaultValue: 'josh',
  );

  /// Shared secret for the API.
  ///
  /// Required once the backend is reachable from the public internet: without
  /// it, anyone with the URL could read the owner's data. Injected at build
  /// time (--dart-define=API_KEY=...) so it is not hardcoded in the repo.
  static const defaultApiKey = String.fromEnvironment(
    'API_KEY',
    defaultValue: '',
  );

  final String baseUrl;
  final String userId;
  final String apiKey;

  static const _timeout = Duration(seconds: 30);

  /// Fallback attempts get a short budget.
  ///
  /// A private address that is not routable (phone off the home WiFi) can hang
  /// until its connect timeout expires, so giving each fallback the full 30s
  /// would freeze the UI for a minute and a half before showing an error.
  /// Short attempts keep the worst case near the original single-request wait.
  static const _fallbackTimeout = Duration(seconds: 6);

  /// Candidate endpoints, tried in order.
  ///
  /// The shipped base URL is the only one that works from anywhere, but it can
  /// be unreachable on a phone whose Tailscale DNS is up while the tunnel is
  /// down, or behind a captive portal. A private-address fallback costs one
  /// failed connect (milliseconds) and can rescue the app outright, so the app
  /// is no longer betting the whole UI on a single path. Override the list at
  /// build time with --dart-define=API_FALLBACKS=url1,url2.
  static const _fallbacks = String.fromEnvironment(
    'API_FALLBACKS',
    defaultValue: 'http://10.11.11.235:8790,http://100.89.180.23:8790',
  );

  List<String> get _bases {
    final list = <String>[
      baseUrl,
      for (final u in _fallbacks.split(','))
        if (u.trim().isNotEmpty && u.trim() != baseUrl) u.trim(),
    ];
    return list;
  }

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'X-User-Id': userId,
        if (apiKey.isNotEmpty) 'X-Api-Key': apiKey,
      };

  /// True when a failure is a connection problem rather than a server reply, so
  /// the caller knows a different endpoint is worth trying.
  static bool _isUnreachable(Object e) =>
      e is TimeoutException ||
      e is SocketException ||
      e is HandshakeException ||
      e is http.ClientException;

  Future<dynamic> _send(String method, String path,
      {Map<String, dynamic>? body}) async {
    final bases = _bases;
    Object? lastError;
    for (var i = 0; i < bases.length; i++) {
      try {
        // Only the primary gets the full budget; fallbacks stay short so a
        // dead private address cannot stall the screen.
        final budget = i == 0 ? _timeout : _fallbackTimeout;
        return await _sendTo(bases[i], method, path, body, budget);
      } catch (e) {
        // Only a transport failure justifies trying the next endpoint. A real
        // HTTP error (401, 404, a rejected payload) means the server answered,
        // so trying another address would just repeat it.
        if (!_isUnreachable(e)) rethrow;
        lastError = e;
      }
    }
    // Every endpoint failed: report the last reason, which is the one most
    // likely to reflect why the final attempt did not work.
    final e = lastError;
    if (e is TimeoutException) {
      throw ApiException('The server took too long to answer. Tap to retry.',
          offline: true);
    }
    if (e is HandshakeException) {
      throw ApiException('Secure connection to the server failed.',
          offline: true);
    }
    throw ApiException(
        'Cannot reach the server. Check your connection.', offline: true);
  }

  Future<dynamic> _sendTo(String base, String method, String path,
      Map<String, dynamic>? body, Duration budget) async {
    final uri = Uri.parse('$base$path');
    try {
      late http.Response res;
      switch (method) {
        case 'GET':
          res = await _http.get(uri, headers: _headers).timeout(budget);
        case 'POST':
          res = await _http
              .post(uri, headers: _headers, body: jsonEncode(body ?? {}))
              .timeout(budget);
        case 'PATCH':
          res = await _http
              .patch(uri, headers: _headers, body: jsonEncode(body ?? {}))
              .timeout(budget);
        case 'DELETE':
          res = await _http.delete(uri, headers: _headers).timeout(budget);
        default:
          throw ApiException('unsupported method $method');
      }
      final decoded = res.body.isEmpty ? null : jsonDecode(res.body);
      if (res.statusCode >= 400) {
        final msg = decoded is Map && decoded['error'] != null
            ? decoded['error'].toString()
            : 'HTTP ${res.statusCode}';
        throw ApiException(msg, statusCode: res.statusCode);
      }
      return decoded;
    } on ApiException {
      rethrow;
    } on TimeoutException {
      // .timeout() throws this, NOT a SocketException. Leaving it uncaught made
      // the whole future fail with an unhandled error, so the caller's
      // `on ApiException` never fired, `_loading` stayed true and the app sat
      // on a blank screen with no message -- the user saw a white app while the
      // widget (whose Kotlin catches everything) correctly said "unreachable".
      rethrow;
    } on HandshakeException {
      rethrow;
    } on SocketException {
      rethrow;
    } on http.ClientException {
      // http wraps TLS/DNS/connection failures in ClientException, which is NOT
      // a SocketException. Catching only SocketException let these escape.
      rethrow;
    } on FormatException {
      // A non-JSON body (a captive portal, a proxy error page) must surface as a
      // message, never as an unhandled crash.
      throw ApiException('The server sent an unexpected reply.',
          offline: true);
    }
  }

  /// The product's central call: one end goal in, a startable plan out.
  Future<GoalResult> createGoal(String title,
      {String? detail, String? targetDate}) async {
    final data = await _send('POST', '/goals', body: {
      'title': title,
      if (detail != null && detail.isNotEmpty) 'detail': detail,
      if (targetDate != null) 'target_date': targetDate,
    });
    return GoalResult.fromJson(Safe.map(data));
  }

  Future<List<Map<String, dynamic>>> goals() async =>
      Safe.mapList(await _send('GET', '/goals'));

  /// Only tasks that can be started right now -- never the whole backlog.
  Future<List<Map<String, dynamic>>> startable() async =>
      Safe.mapList(await _send('GET', '/tasks/startable'));

  Future<Map<String, dynamic>> today() async =>
      Safe.map(await _send('GET', '/today'));

  Future<Map<String, dynamic>> productivity() async =>
      Safe.map(await _send('GET', '/productivity'));

  Future<Map<String, dynamic>> finance() async =>
      Safe.map(await _send('GET', '/finance'));

  Future<void> setTaskStatus(String id, String status) =>
      _send('PATCH', '/tasks', body: {'id': id, 'status': status});

  /// Mark one task done, from a list row.
  Future<void> completeTask(String id) => _send('POST', '/tasks/$id/done');

  /// Undo an accidental completion. The row is un-ticked in place.
  Future<void> reopenTask(String id) => _send('POST', '/tasks/$id/reopen');

  /// Every task under one goal, each flagged startable or blocked.
  Future<Map<String, dynamic>> goalTasks(String goalId) async =>
      Safe.map(await _send('GET', '/goals/$goalId/tasks'));

  /// Add a task to a goal.
  ///
  /// The app can add tasks, but it never PLANS: the ordering, the dependencies
  /// and the next action are decided server-side, so the same list shows up
  /// here, in the widgets and in Hermes's own view of the work.
  Future<Map<String, dynamic>> addTask(
    String goalId,
    String title, {
    int minutes = 30,
    String? why,
    int priority = 3,
  }) async =>
      Safe.map(await _send('POST', '/tasks', body: {
        'goal_id': goalId,
        'title': title,
        'minutes': minutes,
        if (why != null && why.isNotEmpty) 'why': why,
        'priority': priority,
      }));

  Future<void> renameGoal(String goalId, String title) =>
      _send('PATCH', '/goals/$goalId', body: {'title': title});

  Future<void> deleteGoal(String goalId) => _send('DELETE', '/goals/$goalId');

  Future<void> addExpense(num amount, {String? note, String? category}) =>
      _send('POST', '/expenses', body: {
        'amount': amount,
        if (note != null) 'note': note,
        if (category != null) 'category': category,
      });
}

class ApiException implements Exception {
  ApiException(this.message, {this.statusCode, this.offline = false});
  final String message;
  final int? statusCode;
  final bool offline;

  @override
  String toString() => message;
}

/// Null-safe JSON readers.
///
/// Every value from the server is `dynamic`. An `as` cast on a wrong-typed
/// value throws, and a throw inside a build or a loader used to blank the
/// screen. These helpers degrade to sane defaults instead, so a wrong or
/// absent field can never crash the UI.
class Safe {
  /// A decoded-JSON map, or {} when the body is absent or the wrong shape.
  static Map<String, dynamic> map(dynamic v) =>
      v is Map ? Map<String, dynamic>.from(v) : <String, dynamic>{};

  /// A decoded-JSON list of maps, skipping rows of the wrong shape.
  static List<Map<String, dynamic>> mapList(dynamic v) => v is List
      ? v.whereType<Map>().map((m) => Map<String, dynamic>.from(m)).toList()
      : <Map<String, dynamic>>[];

  /// A list of anything (kept for counting), or [] when absent/wrong shape.
  static List list(dynamic v) => v is List ? v : <dynamic>[];

  /// A number field, or null when absent or not a number. Strings are NOT
  /// coerced: silently parsing "abc" as a number would hide server bugs.
  static num? number(dynamic v) => v is num ? v : null;
}

class GoalResult {
  GoalResult({required this.goal, required this.tasks, required this.degraded,
      required this.note});

  final Map<String, dynamic> goal;
  final List<Map<String, dynamic>> tasks;

  /// True when the plan is thin or the model was unreachable. The UI must say
  /// so plainly rather than presenting a degraded plan as a complete one.
  final bool degraded;
  final String note;

  factory GoalResult.fromJson(Map<String, dynamic> j) => GoalResult(
        // `is` checks, not `as` casts: a present-but-wrong-typed value must
        // degrade to the default, never throw and blank the screen.
        goal: Safe.map(j['goal']),
        tasks: Safe.mapList(j['tasks']),
        degraded: j['degraded'] == true,
        note: (j['note'] ?? '').toString(),
      );

  int get totalMinutes =>
      tasks.fold(0, (s, t) => s + (Safe.number(t['minutes'])?.toInt() ?? 0));
}

/// One goal's full checklist, as returned by `GET /goals/<id>/tasks`.
///
/// Parsing lives here rather than in the widget so the classification rules
/// (done / startable / blocked-and-why) are unit-testable without a device.
class GoalTasks {
  GoalTasks({
    required this.goalId,
    required this.tasks,
    required this.total,
    required this.done,
    required this.pctDone,
  });

  final String goalId;
  final List<Map<String, dynamic>> tasks;
  final int total;
  final int done;
  final int pctDone;

  factory GoalTasks.fromJson(Map<String, dynamic> j) {
    final list = Safe.mapList(j['tasks']);
    final done =
        Safe.number(j['done'])?.toInt() ?? list.where((t) => t['status'] == 'done').length;
    final total = Safe.number(j['total'])?.toInt() ?? list.length;
    return GoalTasks(
      goalId: (j['goal_id'] ?? '').toString(),
      tasks: list,
      total: total,
      done: done,
      // Derive rather than trust: a server that omits pct_done would otherwise
      // render an empty progress bar and look like a bug.
      pctDone: Safe.number(j['pct_done'])?.toInt() ??
          (total == 0 ? 0 : ((100 * done) / total).round()),
    );
  }

  /// Rows the user can act on now, in the order the server returned them.
  List<Map<String, dynamic>> get startable =>
      tasks.where((t) => t['startable'] == true).toList();
}

/// A single task row's display state, derived once so every surface agrees.
class TaskState {
  const TaskState({
    required this.id,
    required this.title,
    required this.minutes,
    required this.status,
    required this.startable,
    required this.blockerTitle,
  });

  final String id;
  final String title;
  final int minutes;
  final String status;
  final bool startable;
  final String? blockerTitle;

  bool get isDone => status == 'done';
  bool get isSkipped => status == 'skipped';

  factory TaskState.fromJson(Map<String, dynamic> t) => TaskState(
        id: (t['id'] ?? '').toString(),
        title: (t['title'] ?? '').toString(),
        minutes: Safe.number(t['minutes'])?.toInt() ?? 30,
        status: (t['status'] ?? 'todo').toString(),
        startable: t['startable'] == true,
        blockerTitle: t['blocked_by_title']?.toString(),
      );

  /// The one line under the title that explains the row's state.
  ///
  /// A blocked row must name what it waits on: a greyed row with no
  /// explanation reads as a broken app rather than a dependency.
  String get subtitle {
    if (isDone) return 'Done';
    if (isSkipped) return 'Skipped';
    if (startable) return '$minutes min  ·  ready now';
    if (blockerTitle != null && blockerTitle!.isNotEmpty) {
      return '$minutes min  ·  after “$blockerTitle”';
    }
    return '$minutes min  ·  waiting';
  }
}
