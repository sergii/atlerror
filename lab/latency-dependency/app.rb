require "socket"
require "uri"
require "json"

DEPENDENCY_HOST = ENV.fetch("DEPENDENCY_HOST", "dependency")
DEPENDENCY_PORT = Integer(ENV.fetch("DEPENDENCY_PORT", "9090"))
server = TCPServer.new("0.0.0.0", 8080)

def raw_get(host, port, path)
  socket = TCPSocket.new(host, port)
  socket.write("GET #{path} HTTP/1.1\r\nHost: #{host}\r\nConnection: close\r\n\r\n")
  response = socket.read
  socket.close
  response.split("\r\n\r\n", 2)[1].to_s
end

def respond(socket, status, body, content_type = "application/json")
  socket.write("HTTP/1.1 #{status}\r\nContent-Type: #{content_type}\r\nContent-Length: #{body.bytesize}\r\nConnection: close\r\n\r\n#{body}")
end

loop do
  socket = server.accept
  begin
    request_line = socket.gets
    next unless request_line
    while (line = socket.gets) && line != "\r\n"; end
    path = request_line.split[1] || "/"
    uri = URI.parse(path)
    if uri.path == "/health"
      respond(socket, "200 OK", "ok", "text/plain")
    elsif uri.path == "/work"
      params = URI.decode_www_form(uri.query.to_s).to_h
      delay_ms = Float(params.fetch("delay_ms", "0"))
      started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      raw_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/delay?ms=#{delay_ms}")
      dependency_ms = (Process.clock_gettime(Process::CLOCK_MONOTONIC) - started) * 1000.0
      respond(socket, "200 OK", JSON.generate(dependency_latency_ms: dependency_ms))
    else
      respond(socket, "404 Not Found", JSON.generate(error: "not_found"))
    end
  rescue => error
    respond(socket, "500 Internal Server Error", JSON.generate(error: error.class.name)) rescue nil
  ensure
    socket.close
  end
end
