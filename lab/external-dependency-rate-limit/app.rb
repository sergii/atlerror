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
  lines = head.to_s.lines.map(&:strip)
  status = Integer(lines.first.to_s.split[1])
  headers = {}
  lines.drop(1).each do |line|
    key, value = line.split(":", 2)
    headers[key.downcase] = value.to_s.strip if key
  end
  {status: status, headers: headers, body: body.to_s, response_completed: true}
end

def respond(socket, status, body, headers = {})
  all_headers = {"Content-Type" => "application/json", "Content-Length" => body.bytesize.to_s, "Connection" => "close"}.merge(headers)
  socket.write("HTTP/1.1 #{status}\r\n")
  all_headers.each { |key, value| socket.write("#{key}: #{value}\r\n") }
  socket.write("\r\n#{body}")
end

loop do
  socket = server.accept
  begin
    request_line = socket.gets
    next unless request_line
    while (line = socket.gets) && line != "\r\n"; end
    path = request_line.split[1] || "/"

    if path == "/health"
      respond(socket, "200 OK", JSON.generate(status: "ok"))
    elsif path == "/work"
      dependency = raw_get(DEPENDENCY_HOST, DEPENDENCY_PORT, "/work")
      payload = JSON.generate(
        dependency_status: dependency[:status],
        dependency_response_completed: dependency[:response_completed],
        retry_after: dependency[:headers]["retry-after"],
        rate_limit_remaining: dependency[:headers]["x-ratelimit-remaining"],
      )
      headers = {}
      headers["Retry-After"] = dependency[:headers]["retry-after"] if dependency[:headers]["retry-after"]
      status_line = dependency[:status] == 429 ? "429 Too Many Requests" : "200 OK"
      respond(socket, status_line, payload, headers)
    else
      respond(socket, "404 Not Found", JSON.generate(error: "not_found"))
    end
  rescue => error
    respond(socket, "500 Internal Server Error", JSON.generate(error: error.class.name)) rescue nil
  ensure
    socket.close
  end
end
