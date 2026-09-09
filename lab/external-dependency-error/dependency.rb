require "socket"
require "uri"
require "json"

server = TCPServer.new("0.0.0.0", 9090)
work_status = 200

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

    case uri.path
    when "/health"
      respond(socket, "200 OK", "ok", "text/plain")
    when "/control"
      params = URI.decode_www_form(uri.query.to_s).to_h
      requested = Integer(params.fetch("status", "200"))
      raise ArgumentError, "unsupported status" unless [200, 503].include?(requested)
      work_status = requested
      respond(socket, "200 OK", JSON.generate(work_status: work_status))
    when "/work"
      if work_status == 200
        respond(socket, "200 OK", JSON.generate(status: "ok"))
      else
        respond(socket, "503 Service Unavailable", JSON.generate(error: "service_unavailable"))
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
