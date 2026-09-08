require "socket"
require "json"

APP_HOST = ENV.fetch("APP_HOST", "app")
APP_PORT = Integer(ENV.fetch("APP_PORT", "8080"))
SAMPLES = Integer(ENV.fetch("SAMPLES", "15"))


def request(path)
  started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  socket = TCPSocket.new(APP_HOST, APP_PORT)
  socket.write("GET #{path} HTTP/1.1\r\nHost: #{APP_HOST}\r\nConnection: close\r\n\r\n")
  response = socket.read
  socket.close
  elapsed_ms = (Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000.0
  body = response.split("\r\n\r\n", 2)[1].to_s
  [elapsed_ms, JSON.parse(body)]
end


def percentile50(values)
  sorted = values.sort
  sorted[sorted.length / 2]
end


def phase(delay_ms)
  request_latencies = []
  dependency_latencies = []
  SAMPLES.times do
    request_ms, body = request("/work?delay_ms=#{delay_ms}")
    request_latencies << request_ms
    dependency_latencies << body.fetch("dependency_latency_ms")
  end
  {
    samples: SAMPLES,
    configured_dependency_delay_ms: delay_ms,
    request_p50_ms: percentile50(request_latencies),
    dependency_p50_ms: percentile50(dependency_latencies)
  }
end

baseline = phase(5)
intervention = phase(120)
recovery = phase(5)

request_delta = intervention[:request_p50_ms] - baseline[:request_p50_ms]
dependency_delta = intervention[:dependency_p50_ms] - baseline[:dependency_p50_ms]
recovery_request_delta = (recovery[:request_p50_ms] - baseline[:request_p50_ms]).abs
propagation_error = (request_delta - dependency_delta).abs

thresholds = {
  min_request_delta_ms: 80.0,
  min_dependency_delta_ms: 80.0,
  max_recovery_request_delta_ms: 30.0,
  max_propagation_error_ms: 30.0
}

assertions = {
  dependency_latency_increased: dependency_delta >= thresholds[:min_dependency_delta_ms],
  upstream_latency_increased: request_delta >= thresholds[:min_request_delta_ms],
  recovery_returned_toward_baseline: recovery_request_delta <= thresholds[:max_recovery_request_delta_ms],
  downstream_delay_explains_upstream_delta: propagation_error <= thresholds[:max_propagation_error_ms]
}

result = assertions.values.all? ? "supports" : "contradicts"

evidence = {
  schema_version: "0.1",
  kind: "empirical_evidence",
  experiment: "experiment.latency.external_dependency.ruby_http",
  claims: ["claim.latency.external_dependency.delay_propagates_upstream"],
  environment: {runtime: "ruby", runtime_version: RUBY_VERSION, platform: RUBY_PLATFORM, isolation: "docker_compose"},
  intervention: {action: "increase_downstream_dependency_delay", baseline_delay_ms: 5, intervention_delay_ms: 120, recovery_delay_ms: 5},
  observations: {
    baseline: baseline,
    intervention: intervention,
    recovery: recovery,
    derived: {request_latency_delta_ms: request_delta, dependency_latency_delta_ms: dependency_delta, recovery_request_delta_ms: recovery_request_delta, propagation_error_ms: propagation_error},
    thresholds: thresholds
  },
  assertions: assertions,
  result: result,
  interpretation: "A controlled delay added only to a synchronous downstream HTTP dependency should appear both in measured dependency latency and in upstream end-to-end request latency, then disappear when the delay is removed.",
  limitations: [
    "The application and dependency are synthetic Ruby services on one Docker host.",
    "This validates critical-path latency propagation, not the cause of latency inside a real third-party provider.",
    "Parallel calls, retries, timeouts, caching, asynchronous work, and queueing can change propagation behavior."
  ]
}

puts JSON.generate(evidence)
