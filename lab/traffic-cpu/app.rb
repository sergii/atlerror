require "json"
require "socket"

PORT = Integer(ENV.fetch("PORT", "3000"))
WORK_ITERATIONS = Integer(ENV.fetch("WORK_ITERATIONS", "80000"))

request_count = 0
server = TCPServer.new("0.0.0.0", PORT)


def respond(client, status, body, content_type = "text/plain")
  reason = status == 200 ? "OK" : "Not Found"
  client.write("HTTP/1.1 #{status} #{reason}\r\n")
  client.write("Content-Type: #{content_type}\r\n")
  client.write("Content-Length: #{body.bytesize}\r\n")
  client.write("Connection: close\r\n")
  client.write("\r\n")
  client.write(body)
end

loop do
  client = server.accept

  begin
    request_line = client.gets
    next unless request_line

    _method, path, _version = request_line.split(" ", 3)
    client.gets until $_ == "\r\n" rescue nil

    case path
    when "/health"
      respond(client, 200, "ok")
    when "/metrics"
      body = JSON.generate(
        request_count: request_count,
        cpu_seconds: Process.clock_gettime(Process::CLOCK_PROCESS_CPUTIME_ID)
      )
      respond(client, 200, body, "application/json")
    when "/work"
      checksum = 0
      WORK_ITERATIONS.times do |index|
        checksum = (checksum * 1_664_525 + index + 1_013_904_223) & 0xffff_ffff
      end
      request_count += 1
      respond(client, 200, checksum.to_s)
    else
      respond(client, 404, "not found")
    end
  ensure
    client.close
  end
end
