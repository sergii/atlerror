require "json"

EXPERIMENT_ID = "experiment.gc.pressure.ruby"
CLAIM_ID = "claim.gc.pressure.ruby_allocation_storm_raises_gc_work"
WINDOW_SECONDS = Float(ENV.fetch("WINDOW_SECONDS", "1.5"))
MIN_ALLOCATION_MULTIPLIER = Float(ENV.fetch("MIN_ALLOCATION_MULTIPLIER", "20"))
MIN_GC_SHARE_DELTA = Float(ENV.fetch("MIN_GC_SHARE_DELTA", "0.01"))
MIN_GC_SECONDS = Float(ENV.fetch("MIN_GC_SECONDS", "0.005"))
MIN_CPU_DELTA = Float(ENV.fetch("MIN_CPU_DELTA", "0.40"))
MAX_RECOVERY_CPU_RATIO = Float(ENV.fetch("MAX_RECOVERY_CPU_RATIO", "0.35"))

GC.measure_total_time = true if GC.respond_to?(:measure_total_time=)


def monotonic
  Process.clock_gettime(Process::CLOCK_MONOTONIC)
end


def process_cpu
  Process.clock_gettime(Process::CLOCK_PROCESS_CPUTIME_ID)
end


def gc_seconds
  if GC.respond_to?(:total_time)
    GC.total_time / 1_000_000_000.0
  else
    GC.stat.fetch(:time, 0) / 1_000.0
  end
end


def snapshot
  stats = GC.stat
  {
    wall: monotonic,
    cpu: process_cpu,
    allocated: stats.fetch(:total_allocated_objects),
    gc_count: stats.fetch(:count),
    gc_seconds: gc_seconds
  }
end


def phase(duration, mode)
  before = snapshot
  deadline = before[:wall] + duration
  checksum = 0

  while monotonic < deadline
    if mode == :storm
      500.times do |index|
        payload = ("x" * 128) + index.to_s
        wrapper = [payload, payload.reverse, index]
        checksum ^= wrapper[0].bytesize + wrapper[1].bytesize + wrapper[2]
      end
    else
      5.times do |index|
        payload = "x" * 16
        checksum ^= payload.bytesize + index
      end
      sleep 0.01
    end
  end

  after = snapshot
  wall = after[:wall] - before[:wall]
  cpu = after[:cpu] - before[:cpu]
  gc = after[:gc_seconds] - before[:gc_seconds]
  allocations = after[:allocated] - before[:allocated]
  collections = after[:gc_count] - before[:gc_count]

  {
    wall_seconds: wall,
    cpu_seconds: cpu,
    cpu_utilization: cpu / wall,
    allocations: allocations,
    allocation_rate: allocations / wall,
    gc_collections: collections,
    gc_seconds: gc,
    gc_cpu_share: cpu.positive? ? gc / cpu : 0.0,
    checksum: checksum
  }
end

GC.start(full_mark: true, immediate_sweep: true)
baseline = phase(WINDOW_SECONDS, :baseline)
GC.start(full_mark: true, immediate_sweep: true)
intervention = phase(WINDOW_SECONDS, :storm)
GC.start(full_mark: true, immediate_sweep: true)
recovery = phase(WINDOW_SECONDS, :baseline)

allocation_multiplier = intervention[:allocation_rate] / [baseline[:allocation_rate], 1.0].max
gc_share_delta = intervention[:gc_cpu_share] - baseline[:gc_cpu_share]
cpu_delta = intervention[:cpu_utilization] - baseline[:cpu_utilization]
recovery_cpu_ratio = recovery[:cpu_utilization] / [intervention[:cpu_utilization], 0.000001].max

assertions = {
  allocation_rate_increased: allocation_multiplier >= MIN_ALLOCATION_MULTIPLIER,
  gc_share_increased: gc_share_delta >= MIN_GC_SHARE_DELTA,
  gc_time_material: intervention[:gc_seconds] >= MIN_GC_SECONDS,
  cpu_increased: cpu_delta >= MIN_CPU_DELTA,
  recovery_cpu_dropped: recovery_cpu_ratio <= MAX_RECOVERY_CPU_RATIO
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
    platform: RUBY_PLATFORM,
    isolation: "docker"
  },
  intervention: {
    action: "increase_short_lived_object_allocation",
    window_seconds: WINDOW_SECONDS
  },
  observations: {
    baseline: baseline,
    intervention: intervention,
    recovery: recovery,
    derived: {
      allocation_rate_multiplier: allocation_multiplier,
      gc_cpu_share_delta: gc_share_delta,
      cpu_utilization_delta: cpu_delta,
      recovery_cpu_ratio: recovery_cpu_ratio
    },
    thresholds: {
      min_allocation_multiplier: MIN_ALLOCATION_MULTIPLIER,
      min_gc_share_delta: MIN_GC_SHARE_DELTA,
      min_gc_seconds: MIN_GC_SECONDS,
      min_cpu_delta: MIN_CPU_DELTA,
      max_recovery_cpu_ratio: MAX_RECOVERY_CPU_RATIO
    }
  },
  assertions: assertions,
  result: result,
  interpretation: "A sustained short-lived Ruby allocation storm should materially raise allocation rate, force measurable garbage-collection work, and increase process CPU compared with a low-allocation baseline. Returning to the low-allocation workload should reduce CPU again.",
  limitations: [
    "The experiment validates a synthetic short-lived-allocation mechanism, not a universal diagnosis for high CPU.",
    "GC CPU share is derived from Ruby GC total time divided by process CPU time for each phase.",
    "Exact ratios depend on Ruby GC behavior, container scheduling, heap state, and runner hardware."
  ]
}

puts JSON.generate(evidence)
exit(assertions.values.all? ? 0 : 1)
