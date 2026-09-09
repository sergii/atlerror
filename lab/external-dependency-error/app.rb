require "socket"
require "json"

DEPENDENCY_HOST = ENV.fetch("DEPENDENCY_HOST", "dependency")
DEPENDENCY_PORT = Integer(ENV.fetch("DEPENDENCY_PORT", "9090"))
server = TCPServer.new("0.0.0.0", 8080)

def raw_get(host, port, path)
  socket = TCPSocket.new(host, port)
  socket.write("GET #{path} HTTP/1.1\r\nHost: #{host}\r\nConnection: close\r\n\r\n")
  response = socket.read
  socket.close
  head, body = response.split("\r\n\r\n", 2)
  status = Integer(head.to_s.lines.first.to_s.split[1])
  [status, body.to_s]
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

    if path == "/health"
      respond(socket, "200 OK", "ok", "text/plain")
    elsif path == "/work"
      dependency_status, dependency_body = raw_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
      upstream_status = dependency_status >= 500 ? "502 Bad Gateway" : "200 OK"
      body = JSON.generate(
        dependency_status: dependency_status,
        dependency_response_completed: true,
        dependency_body: dependency_body,
      )
      respond(socket, upstream_status, body)
    else
      respond(socket, "404 Not Found", JSON.generate(error: "not_found"))
    end
  rescue => error
    respond(socket, "500 Internal Server Error", JSON.generate(error: error.class.name)) rescue nil
  ensure
    socket.close
  end
end
