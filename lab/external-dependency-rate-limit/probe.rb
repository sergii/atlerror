require "socket"
require "json"
require "rbconfig"

DEPENDENCY_HOST = ENV.fetch("DEPENDENCY_HOST", "dependency")
DEPENDENCY_PORT = Integer(ENV.fetch("DEPENDENCY_PORT", "9090"))
APP_HOST = ENV.fetch("APP_HOST", "app")
APP_PORT = Integer(ENV.fetch("APP_PORT", "8080"))

EXPERIMENT_ID = "experiment.external_dependency.rate_limiting.ruby_http"
CLAIM_ID = "claim.external_dependency.rate_limiting.http_429_after_threshold"

def http_get(host, port, path)
  started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  socket = nil
  result = {
    host: host,
    port: port,
    path: path,
    connected: false,
    response_completed: false,
    status: nil,
    headers: {},
    body: nil,
    error_class: nil,
    error_message: nil,
  }

  begin
    socket = TCPSocket.new(host, port)
    result[:connected] = true
    socket.write("GET #{path} HTTP/1.1\r\nHost: #{host}\r\nConnection: close\r\n\r\n")
    response = socket.read
    head, body = response.split("\r\n\r\n", 2)
    lines = head.to_s.lines.map(&:strip)
    result[:status] = Integer(lines.first.to_s.split[1])
    lines.drop(1).each do |line|
      key, value = line.split(":", 2)
      result[:headers][key.downcase] = value.to_s.strip if key
    end
    result[:body] = body.to_s
    result[:response_completed] = true
  rescue => error
    result[:error_class] = error.class.name
    result[:error_message] = error.message
  ensure
    socket&.close
    result[:elapsed_ms] = (Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000.0
  end

  result
end

def parsed_json(result)
  JSON.parse(result[:body].to_s)
rescue JSON::ParserError
  {}
end

baseline_reset = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/control/reset")
baseline_health = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/health")
allowed_one = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
allowed_two = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
limited = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
intervention_health = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/health")
upstream_limited = http_get(APP_HOST, APP_PORT, "/work")
upstream_body = parsed_json(upstream_limited)

recovery_reset = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/control/reset")
recovery_health = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/health")
recovery_upstream = http_get(APP_HOST, APP_PORT, "/work")
recovery_body = parsed_json(recovery_upstream)

assertions = {
  baseline_reset_succeeds: baseline_reset[:status] == 200,
  dependency_health_stays_ok_before_limit: baseline_health[:status] == 200,
  first_request_within_allowance_succeeds: allowed_one[:status] == 200,
  second_request_within_allowance_succeeds: allowed_two[:status] == 200,
  third_request_is_rate_limited: limited[:status] == 429,
  rate_limited_response_completes: limited[:connected] && limited[:response_completed],
  rate_limited_response_has_no_transport_error: limited[:error_class].nil?,
  rate_limited_response_has_retry_after: limited[:headers]["retry-after"] == "1",
  rate_limit_remaining_is_zero: limited[:headers]["x-ratelimit-remaining"] == "0",
  dependency_health_stays_ok_while_rate_limited: intervention_health[:status] == 200,
  upstream_surfaces_rate_limit: upstream_limited[:status] == 429,
  upstream_preserves_dependency_429: upstream_body["dependency_status"] == 429,
  upstream_preserves_retry_after: upstream_body["retry_after"] == "1",
  recovery_reset_succeeds: recovery_reset[:status] == 200,
  recovery_health_is_ok: recovery_health[:status] == 200,
  recovery_request_succeeds: recovery_upstream[:status] == 200,
  recovery_records_dependency_success: recovery_body["dependency_status"] == 200,
}

result = assertions.values.all? ? "supports" : "inconclusive"

evidence = {
  schema_version: "0.1",
  kind: "empirical_evidence",
  experiment: EXPERIMENT_ID,
  claims: [CLAIM_ID],
  environment: {
    runtime: "ruby",
    runtime_version: RUBY_VERSION,
    operating_system: RbConfig::CONFIG["host_os"],
    platform: RUBY_PLATFORM,
    isolation: "docker_compose",
  },
  intervention: {
    action: "exceed_deterministic_two_request_dependency_allowance_then_reset_policy_state",
    limit: 2,
    limited_status: 429,
    retry_after_seconds: 1,
  },
  observations: {
    baseline: {
      reset: baseline_reset,
      dependency_health: baseline_health,
      allowed_request_one: allowed_one,
      allowed_request_two: allowed_two,
    },
    intervention: {
      limited_request: limited,
      dependency_health: intervention_health,
      upstream_request: upstream_limited,
      upstream_body: upstream_body,
    },
    recovery: {
      reset: recovery_reset,
      dependency_health: recovery_health,
      upstream_request: recovery_upstream,
      upstream_body: recovery_body,
    },
  },
  assertions: assertions,
  result: result,
  interpretation: "The same reachable dependency accepts two requests within its configured allowance, then returns a completed HTTP 429 response with Retry-After and zero remaining quota while its health endpoint remains 200. The upstream application preserves and surfaces the rate-limit response. Resetting the policy state restores successful requests without changing DNS, TCP, TLS, or endpoint identity.",
  limitations: [
    "The dependency and upstream application are synthetic Ruby processes on one Docker network.",
    "The rate-limit window is reset deterministically through a control endpoint rather than by waiting for real wall-clock expiry.",
    "The experiment validates explicit HTTP throttling semantics, not provider-specific quota algorithms, distributed counter consistency, or production retry safety.",
  ],
}

puts JSON.generate(evidence)
