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
import 'dart:io' show SocketException;

import 'package:http/http.dart' as http;

class Api {
  Api({String? baseUrl, required this.userId, String? apiKey})
      : baseUrl = baseUrl ?? defaultBaseUrl,
        apiKey = apiKey ?? defaultApiKey;

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

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'X-User-Id': userId,
        if (apiKey.isNotEmpty) 'X-Api-Key': apiKey,
      };

  Future<dynamic> _send(String method, String path,
      {Map<String, dynamic>? body}) async {
    final uri = Uri.parse('$baseUrl$path');
    try {
      late http.Response res;
      switch (method) {
        case 'GET':
          res = await http.get(uri, headers: _headers).timeout(_timeout);
        case 'POST':
          res = await http
              .post(uri, headers: _headers, body: jsonEncode(body ?? {}))
              .timeout(_timeout);
        case 'PATCH':
          res = await http
              .patch(uri, headers: _headers, body: jsonEncode(body ?? {}))
              .timeout(_timeout);
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
    } on SocketException {
      throw ApiException(
          'Cannot reach the server. Are you on Tailscale?', offline: true);
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
    return GoalResult.fromJson(data as Map<String, dynamic>);
  }

  Future<List<Map<String, dynamic>>> goals() async =>
      ((await _send('GET', '/goals')) as List)
          .cast<Map<String, dynamic>>();

  /// Only tasks that can be started right now -- never the whole backlog.
  Future<List<Map<String, dynamic>>> startable() async =>
      ((await _send('GET', '/tasks/startable')) as List)
          .cast<Map<String, dynamic>>();

  Future<Map<String, dynamic>> today() async =>
      (await _send('GET', '/today')) as Map<String, dynamic>;

  Future<Map<String, dynamic>> productivity() async =>
      (await _send('GET', '/productivity')) as Map<String, dynamic>;

  Future<Map<String, dynamic>> finance() async =>
      (await _send('GET', '/finance')) as Map<String, dynamic>;

  Future<void> setTaskStatus(String id, String status) =>
      _send('PATCH', '/tasks', body: {'id': id, 'status': status});

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
        goal: (j['goal'] as Map?)?.cast<String, dynamic>() ?? {},
        tasks: ((j['tasks'] as List?) ?? [])
            .cast<Map<String, dynamic>>(),
        degraded: j['degraded'] == true,
        note: (j['note'] ?? '').toString(),
      );

  int get totalMinutes =>
      tasks.fold(0, (s, t) => s + ((t['minutes'] as num?)?.toInt() ?? 0));
}
