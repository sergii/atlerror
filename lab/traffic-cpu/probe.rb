require "json"
require "net/http"
require "uri"

EXPERIMENT_ID = "experiment.traffic.increase.ruby_http"
CLAIM_ID = "claim.traffic.increase.fixed_cost_http_raises_cpu"
TARGET_URL = ENV.fetch("TARGET_URL", "http://app:3000")
BASELINE_RPS = Float(ENV.fetch("BASELINE_RPS", "8"))
INTERVENTION_RPS = Float(ENV.fetch("INTERVENTION_RPS", "70"))
RECOVERY_RPS = Float(ENV.fetch("RECOVERY_RPS", "8"))
WINDOW_SECONDS = Float(ENV.fetch("WINDOW_SECONDS", "2.5"))
MIN_RPS_MULTIPLIER = Float(ENV.fetch("MIN_RPS_MULTIPLIER", "3.0"))
MIN_CPU_DELTA = Float(ENV.fetch("MIN_CPU_DELTA", "0.15"))
MIN_INTERVENTION_CPU = Float(ENV.fetch("MIN_INTERVENTION_CPU", "0.20"))
MAX_RECOVERY_CPU_RATIO = Float(ENV.fetch("MAX_RECOVERY_CPU_RATIO", "0.60"))

BASE_URI = URI(TARGET_URL)


def request(path)
  uri = BASE_URI + path
  Net::HTTP.start(uri.host, uri.port, open_timeout: 2, read_timeout: 5) do |http|
    response = http.get(uri.request_uri)
    raise "HTTP #{response.code} for #{path}" unless response.is_a?(Net::HTTPSuccess)
    response.body
  end
end


def metrics
  JSON.parse(request("/metrics"), symbolize_names: true)
end


def run_window(target_rps, duration)
  before = metrics
  started_at = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  deadline = started_at + duration
  sent = 0

  while Process.clock_gettime(Process::CLOCK_MONOTONIC) < deadline
    request("/work")
    sent += 1
    next_at = started_at + (sent / target_rps)
    remaining = next_at - Process.clock_gettime(Process::CLOCK_MONOTONIC)
    sleep(remaining) if remaining.positive?
  end

  finished_at = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  after = metrics
  wall_seconds = finished_at - started_at
  completed_requests = after[:request_count] - before[:request_count]
  cpu_seconds = after[:cpu_seconds] - before[:cpu_seconds]

  {
    target_rps: target_rps,
    request_rate: completed_requests / wall_seconds,
    requests: completed_requests,
    cpu_seconds: cpu_seconds,
    cpu_utilization: cpu_seconds / wall_seconds,
    wall_seconds: wall_seconds
  }
end

20.times { request("/work") }
sleep 0.2

baseline = run_window(BASELINE_RPS, WINDOW_SECONDS)
intervention = run_window(INTERVENTION_RPS, WINDOW_SECONDS)
recovery = run_window(RECOVERY_RPS, WINDOW_SECONDS)

rps_multiplier = intervention[:request_rate] / baseline[:request_rate]
cpu_delta = intervention[:cpu_utilization] - baseline[:cpu_utilization]
recovery_cpu_ratio = recovery[:cpu_utilization] / intervention[:cpu_utilization]

assertions = {
  request_rate_increased: rps_multiplier >= MIN_RPS_MULTIPLIER,
  cpu_increased: cpu_delta >= MIN_CPU_DELTA,
  intervention_cpu_material: intervention[:cpu_utilization] >= MIN_INTERVENTION_CPU,
  recovery_cpu_dropped: recovery_cpu_ratio <= MAX_RECOVERY_CPU_RATIO
}

result = assertions.values.all? ? "supports" : "inconclusive"

evidence = {
  kind: "empirical_evidence",
  schema_version: "0.1",
  experiment: EXPERIMENT_ID,
  claims: [CLAIM_ID],
  environment: {
    runtime: "ruby",
    runtime_version: RUBY_VERSION,
    platform: RUBY_PLATFORM,
    isolation: "docker_compose"
  },
  intervention: {
    action: "increase_http_request_rate",
    baseline_target_rps: BASELINE_RPS,
    intervention_target_rps: INTERVENTION_RPS,
    recovery_target_rps: RECOVERY_RPS,
    window_seconds: WINDOW_SECONDS
  },
  observations: {
    baseline: baseline,
    intervention: intervention,
    recovery: recovery,
    derived: {
      request_rate_multiplier: rps_multiplier,
      cpu_utilization_delta: cpu_delta,
      recovery_cpu_ratio: recovery_cpu_ratio
    },
    thresholds: {
      min_rps_multiplier: MIN_RPS_MULTIPLIER,
      min_cpu_delta: MIN_CPU_DELTA,
      min_intervention_cpu: MIN_INTERVENTION_CPU,
      max_recovery_cpu_ratio: MAX_RECOVERY_CPU_RATIO
    }
  },
  assertions: assertions,
  result: result,
  interpretation: "With fixed synthetic CPU work per HTTP request, raising sustained request rate should raise the service process CPU utilization; returning traffic toward baseline should reduce CPU again.",
  limitations: [
    "This is a controlled single-route workload and does not establish a universal linear relationship between production traffic and CPU.",
    "The service and load generator share one Docker host, so scheduler noise can affect exact values.",
    "The experiment does not model autoscaling, heterogeneous route costs, caching, external I/O, or distributed load balancing."
  ]
}

puts JSON.generate(evidence)
exit(assertions.values.all? ? 0 : 1)
