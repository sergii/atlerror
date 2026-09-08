require "json"

EXPERIMENT_ID = "experiment.cpu.busy_loop.ruby"
CLAIM_ID = "claim.cpu.busy_loop.ruby_consumes_one_core"
BASELINE_SECONDS = Float(ENV.fetch("BASELINE_SECONDS", "0.6"))
BUSY_SECONDS = Float(ENV.fetch("BUSY_SECONDS", "1.2"))
MIN_BUSY_UTILIZATION = Float(ENV.fetch("MIN_BUSY_UTILIZATION", "0.75"))
MIN_UTILIZATION_DELTA = Float(ENV.fetch("MIN_UTILIZATION_DELTA", "0.60"))
MIN_USER_SHARE = Float(ENV.fetch("MIN_USER_SHARE", "0.85"))


def monotonic
  Process.clock_gettime(Process::CLOCK_MONOTONIC)
end


def cpu_snapshot
  times = Process.times
  {
    user: times.utime,
    system: times.stime
  }
end


def measure_idle(duration)
  start_wall = monotonic
  start_cpu = cpu_snapshot
  sleep(duration)
  finish_wall = monotonic
  finish_cpu = cpu_snapshot

  summarize(start_wall, finish_wall, start_cpu, finish_cpu)
end


def measure_busy(duration)
  start_wall = monotonic
  start_cpu = cpu_snapshot
  deadline = start_wall + duration
  iterations = 0
  value = 1

  while monotonic < deadline
    value = ((value * 1_664_525) + 1_013_904_223) & 0xffff_ffff
    iterations += 1
  end

  finish_wall = monotonic
  finish_cpu = cpu_snapshot
  summarize(start_wall, finish_wall, start_cpu, finish_cpu).merge(
    iterations: iterations,
    checksum: value
  )
end


def summarize(start_wall, finish_wall, start_cpu, finish_cpu)
  wall = finish_wall - start_wall
  user = finish_cpu[:user] - start_cpu[:user]
  system = finish_cpu[:system] - start_cpu[:system]
  cpu = user + system

  {
    wall_seconds: wall,
    cpu_seconds: cpu,
    user_seconds: user,
    system_seconds: system,
    cpu_utilization: cpu / wall,
    user_share: cpu.positive? ? user / cpu : 0.0
  }
end

baseline = measure_idle(BASELINE_SECONDS)
intervention = measure_busy(BUSY_SECONDS)
utilization_delta = intervention[:cpu_utilization] - baseline[:cpu_utilization]

assertions = {
  busy_utilization_high: intervention[:cpu_utilization] >= MIN_BUSY_UTILIZATION,
  utilization_increased: utilization_delta >= MIN_UTILIZATION_DELTA,
  user_space_dominant: intervention[:user_share] >= MIN_USER_SHARE
}

result = assertions.values.all? ? "supports" : "inconclusive"

puts JSON.generate(
  schema_version: "0.1",
  kind: "empirical_evidence",
  experiment: EXPERIMENT_ID,
  claims: [CLAIM_ID],
  environment: {
    runtime: "ruby",
    runtime_version: RUBY_VERSION,
    platform: RUBY_PLATFORM,
    isolation: "docker"
  },
  intervention: {
    action: "tight_non_blocking_loop",
    duration_seconds: BUSY_SECONDS
  },
  observations: {
    baseline: baseline,
    intervention: intervention,
    deltas: {
      cpu_utilization: utilization_delta
    },
    thresholds: {
      min_busy_utilization: MIN_BUSY_UTILIZATION,
      min_utilization_delta: MIN_UTILIZATION_DELTA,
      min_user_share: MIN_USER_SHARE
    }
  },
  assertions: assertions,
  result: result,
  interpretation: "A tight Ruby loop should spend most of its wall-clock interval executing on CPU rather than blocking. Under a one-CPU Docker quota, process CPU utilization should approach one logical core and user-space CPU should dominate system CPU.",
  limitations: [
    "This experiment does not validate hot-stack repetition; profiling is a separate diagnostic probe.",
    "Scheduler noise and virtualization can affect exact utilization values."
  ]
)

exit(assertions.values.all? ? 0 : 1)
