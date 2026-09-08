require "json"

WINDOW_SECONDS = Float(ENV.fetch("WINDOW_SECONDS", "1.5"))
TARGET = "/tmp/atlerror-kernel-work-target"
File.write(TARGET, "atlerror\n")


def measure_phase(duration)
  wall_start = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  cpu_start = Process.times
  count = yield(duration)
  cpu_end = Process.times
  wall_end = Process.clock_gettime(Process::CLOCK_MONOTONIC)

  wall = wall_end - wall_start
  user = cpu_end.utime - cpu_start.utime
  system = cpu_end.stime - cpu_start.stime
  total = user + system

  {
    wall_seconds: wall,
    operations: count,
    user_cpu_seconds: user,
    system_cpu_seconds: system,
    total_cpu_seconds: total,
    cpu_utilization: wall.positive? ? total / wall : 0.0,
    system_cpu_share: total.positive? ? system / total : 0.0
  }
end


def user_compute(duration)
  deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + duration
  count = 0
  value = 0x1234_5678

  while Process.clock_gettime(Process::CLOCK_MONOTONIC) < deadline
    2_000.times do |index|
      value = ((value * 1_664_525) + index + 1_013_904_223) & 0xffff_ffff
    end
    count += 2_000
  end

  count ^ value
end


def syscall_work(duration)
  deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + duration
  count = 0

  while Process.clock_gettime(Process::CLOCK_MONOTONIC) < deadline
    500.times do
      File.stat(TARGET)
      count += 1
    end
  end

  count
end

baseline = measure_phase(WINDOW_SECONDS) { |duration| user_compute(duration) }
intervention = measure_phase(WINDOW_SECONDS) { |duration| syscall_work(duration) }
recovery = measure_phase(WINDOW_SECONDS) { |duration| user_compute(duration) }

system_share_delta = intervention[:system_cpu_share] - baseline[:system_cpu_share]
recovery_system_ratio = intervention[:system_cpu_share].positive? ? recovery[:system_cpu_share] / intervention[:system_cpu_share] : 1.0

thresholds = {
  min_system_share_delta: 0.10,
  min_intervention_system_share: 0.15,
  min_intervention_cpu_utilization: 0.60,
  max_recovery_system_ratio: 0.60,
  min_syscall_operations: 50_000
}

assertions = {
  system_share_increased: system_share_delta >= thresholds[:min_system_share_delta],
  system_share_material: intervention[:system_cpu_share] >= thresholds[:min_intervention_system_share],
  cpu_sustained: intervention[:cpu_utilization] >= thresholds[:min_intervention_cpu_utilization],
  recovery_system_share_dropped: recovery_system_ratio <= thresholds[:max_recovery_system_ratio],
  syscall_volume_material: intervention[:operations] >= thresholds[:min_syscall_operations]
}

result = assertions.values.all? ? "supports" : "contradicts"

evidence = {
  schema_version: "0.1",
  kind: "empirical_evidence",
  experiment: "experiment.kernel.work.ruby",
  claims: ["claim.kernel.work.ruby_syscall_storm_raises_system_cpu_share"],
  environment: {
    runtime: "ruby",
    runtime_version: RUBY_VERSION,
    platform: RUBY_PLATFORM,
    isolation: "docker"
  },
  intervention: {
    action: "replace_user_compute_with_filesystem_metadata_syscalls",
    syscall: "stat",
    window_seconds: WINDOW_SECONDS
  },
  observations: {
    baseline: baseline,
    intervention: intervention,
    recovery: recovery,
    derived: {
      system_cpu_share_delta: system_share_delta,
      recovery_system_share_ratio: recovery_system_ratio
    },
    thresholds: thresholds
  },
  assertions: assertions,
  result: result,
  interpretation: "Replacing a mostly user-space Ruby compute loop with a high-volume filesystem metadata syscall workload should materially increase the process system-CPU share while keeping the process CPU-active; returning to user-space compute should reduce system-CPU share again.",
  limitations: [
    "This reproduces cached filesystem metadata syscall overhead, not networking, interrupts, context switching, or physical block-device I/O.",
    "The experiment distinguishes user and system CPU using process accounting and does not attribute individual kernel functions.",
    "Exact ratios depend on Linux kernel, filesystem, Ruby, container runtime, and host scheduling."
  ]
}

puts JSON.generate(evidence)
