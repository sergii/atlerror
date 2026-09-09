require "socket"
require "uri"
require "json"

LIMIT = 2
state = {count: 0}
server = TCPServer.new("0.0.0.0", 9090)

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
    uri = URI.parse(path)

    case uri.path
    when "/health"
      respond(socket, "200 OK", JSON.generate(status: "ok"), "Content-Type" => "application/json")
    when "/control/reset"
      state[:count] = 0
      respond(socket, "200 OK", JSON.generate(reset: true, limit: LIMIT))
    when "/work"
      state[:count] += 1
      remaining = [LIMIT - state[:count], 0].max
      headers = {
        "X-RateLimit-Limit" => LIMIT.to_s,
        "X-RateLimit-Remaining" => remaining.to_s,
      }
      if state[:count] <= LIMIT
        respond(socket, "200 OK", JSON.generate(status: "ok", request_number: state[:count]), headers)
      else
        headers["Retry-After"] = "1"
        respond(socket, "429 Too Many Requests", JSON.generate(error: "rate_limited", request_number: state[:count]), headers)
      end
    else
      respond(socket, "404 Not Found", JSON.generate(error: "not_found"))
    end
  rescue => error
    respond(socket, "500 Internal Server Error", JSON.generate(error: error.class.name)) rescue nil
  ensure
    socket.close
  end
end
