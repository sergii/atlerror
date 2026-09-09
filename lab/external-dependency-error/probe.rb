require "socket"
require "json"
require "rbconfig"

DEPENDENCY_HOST = ENV.fetch("DEPENDENCY_HOST", "dependency")
DEPENDENCY_PORT = Integer(ENV.fetch("DEPENDENCY_PORT", "9090"))
APP_HOST = ENV.fetch("APP_HOST", "app")
APP_PORT = Integer(ENV.fetch("APP_PORT", "8080"))

EXPERIMENT_ID = "experiment.external_dependency.service_error.ruby_http"
CLAIM_ID = "claim.external_dependency.service_error.http_503_surfaces_upstream_failure"

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
    result[:status] = Integer(head.to_s.lines.first.to_s.split[1])
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

def set_dependency_status(status)
  http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/control?status=#{status}")
end

baseline_control = set_dependency_status(200)
baseline_health = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/health")
baseline_dependency = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
baseline_upstream = http_get(APP_HOST, APP_PORT, "/work")

intervention_control = set_dependency_status(503)
intervention_health = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/health")
intervention_dependency = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
intervention_upstream = http_get(APP_HOST, APP_PORT, "/work")
intervention_upstream_body = parsed_json(intervention_upstream)

recovery_control = set_dependency_status(200)
recovery_dependency = http_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
recovery_upstream = http_get(APP_HOST, APP_PORT, "/work")

assertions = {
  baseline_control_succeeds: baseline_control[:status] == 200,
  baseline_dependency_health_is_ok: baseline_health[:status] == 200,
  baseline_dependency_work_succeeds: baseline_dependency[:status] == 200,
  baseline_upstream_succeeds: baseline_upstream[:status] == 200,
  intervention_control_succeeds: intervention_control[:status] == 200,
  intervention_dependency_remains_reachable: intervention_health[:status] == 200 && intervention_health[:connected],
  intervention_dependency_http_exchange_completes: intervention_dependency[:connected] && intervention_dependency[:response_completed],
  intervention_dependency_returns_503: intervention_dependency[:status] == 503,
  intervention_has_no_transport_error: intervention_dependency[:error_class].nil?,
  intervention_upstream_returns_failure: intervention_upstream[:status] == 502,
  intervention_upstream_records_dependency_503: intervention_upstream_body["dependency_status"] == 503,
  intervention_upstream_records_completed_dependency_response: intervention_upstream_body["dependency_response_completed"] == true,
  recovery_control_succeeds: recovery_control[:status] == 200,
  recovery_dependency_succeeds: recovery_dependency[:status] == 200,
  recovery_upstream_succeeds: recovery_upstream[:status] == 200,
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
    action: "change_reachable_dependency_work_response_from_http_200_to_http_503_then_restore_200",
    dependency_health_path: "/health",
    dependency_work_path: "/work",
    baseline_status: 200,
    intervention_status: 503,
    recovery_status: 200,
  },
  observations: {
    baseline: {
      dependency_health: baseline_health,
      dependency_work: baseline_dependency,
      upstream_work: baseline_upstream,
    },
    intervention: {
      dependency_health: intervention_health,
      dependency_work: intervention_dependency,
      upstream_work: intervention_upstream,
      upstream_body: intervention_upstream_body,
    },
    recovery: {
      dependency_work: recovery_dependency,
      upstream_work: recovery_upstream,
    },
  },
  assertions: assertions,
  result: result,
  interpretation: "The same reachable downstream HTTP service remains healthy at the transport and health-check level while its work endpoint changes from 200 to an explicit 503 response. The upstream application receives that completed HTTP failure and surfaces a 502 while preserving the dependency status as evidence. Restoring the work endpoint to 200 recovers both downstream and upstream requests.",
  limitations: [
    "The dependency and upstream application are synthetic Ruby processes on one Docker network.",
    "This validates explicit HTTP 503 service-level failure, not provider-internal causes or the correctness of any retry policy.",
    "It does not model DNS, TCP, TLS, HTTP timeout, malformed responses, partial bodies, rate limiting, or non-HTTP protocols.",
  ],
}

puts JSON.generate(evidence)
