require "json"

EXPERIMENT_ID = "experiment.memory.retention.ruby"
CLAIM_ID = "claim.memory.retention.ruby_live_set_and_rss_grow"
OBJECT_COUNT = Integer(ENV.fetch("OBJECT_COUNT", "60000"))
PAYLOAD_BYTES = Integer(ENV.fetch("PAYLOAD_BYTES", "1024"))
MIN_RSS_DELTA_KB = Integer(ENV.fetch("MIN_RSS_DELTA_KB", "30000"))
MIN_LIVE_DELTA = Integer(ENV.fetch("MIN_LIVE_DELTA", "40000"))


def rss_kb
  status = File.read("/proc/self/status")
  match = status.match(/^VmRSS:\s+(\d+)\s+kB$/)
  raise "VmRSS unavailable" unless match

  Integer(match[1])
end


def cgroup_memory_bytes
  path = "/sys/fs/cgroup/memory.current"
  return nil unless File.exist?(path)

  Integer(File.read(path).strip)
end


def snapshot
  GC.start(full_mark: true, immediate_sweep: true)
  stats = GC.stat

  {
    rss_kb: rss_kb,
    heap_live_objects: stats.fetch(:heap_live_slots),
    heap_available_slots: stats.fetch(:heap_available_slots),
    gc_count: stats.fetch(:count),
    cgroup_memory_bytes: cgroup_memory_bytes
  }
end

baseline = snapshot

retained = Array.new(OBJECT_COUNT) do |index|
  ("x" * PAYLOAD_BYTES) + index.to_s
end

intervention = snapshot

rss_delta_kb = intervention[:rss_kb] - baseline[:rss_kb]
live_delta = intervention[:heap_live_objects] - baseline[:heap_live_objects]

assertions = {
  rss_increased: rss_delta_kb >= MIN_RSS_DELTA_KB,
  live_objects_increased: live_delta >= MIN_LIVE_DELTA
}

retained.clear
retained = nil
recovery = snapshot

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
    action: "retain_objects",
    object_count: OBJECT_COUNT,
    payload_bytes: PAYLOAD_BYTES
  },
  observations: {
    baseline: baseline,
    intervention: intervention,
    recovery: recovery,
    deltas: {
      rss_kb: rss_delta_kb,
      heap_live_objects: live_delta
    },
    thresholds: {
      min_rss_delta_kb: MIN_RSS_DELTA_KB,
      min_live_delta: MIN_LIVE_DELTA
    }
  },
  assertions: assertions,
  result: result,
  interpretation: "Retaining reachable Ruby objects should increase both the managed live set and process RSS. Releasing the references should reduce the managed live set; RSS is recorded but is not required to return immediately because allocator behavior is separate from object reachability.",
  limitations: [
    "This synthetic reproduction does not prove that every high-RSS incident is caused by retained Ruby objects.",
    "Allocator and operating-system behavior influence RSS recovery independently from Ruby object reachability."
  ]
)

exit(assertions.values.all? ? 0 : 1)
