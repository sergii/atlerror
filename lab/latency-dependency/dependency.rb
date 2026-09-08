require "socket"
require "uri"
require "json"

server = TCPServer.new("0.0.0.0", 9090)

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
    elsif uri.path == "/delay"
      params = URI.decode_www_form(uri.query.to_s).to_h
      delay_ms = Float(params.fetch("ms", "0"))
      sleep(delay_ms / 1000.0)
      respond(socket, "200 OK", JSON.generate(delay_ms: delay_ms))
    else
      respond(socket, "404 Not Found", JSON.generate(error: "not_found"))
    end
  rescue => error
    respond(socket, "500 Internal Server Error", JSON.generate(error: error.class.name)) rescue nil
  ensure
    socket.close
  end
end
