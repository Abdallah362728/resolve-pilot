--[[
ResolvePilot Bridge (Lua) -- runs INSIDE DaVinci Resolve: Workspace > Scripts > ResolvePilot Bridge.

DaVinci Resolve (free) refuses external scripting connections, and recent free builds only list
Lua scripts in the Scripts menu and run them in a sandbox without io/os.execute. Menu scripts do
get a live `resolve` object and LuaJIT's FFI, so this script exposes the Resolve API on a
localhost-only, token-protected TCP socket. The ResolvePilot MCP server connects to it.

Protocol: newline-delimited JSON over TCP on 127.0.0.1.
  request : {"id": n, "token": "...", "op": "...", ...}
  response: {"id": n, "ok": true, "result": ...} | {"id": n, "ok": false, "error": "..."}
Ops: hello, call {h, m, a}, attr {h, n}, release {hs}, exec_lua {code, args}, ping, shutdown.
Resolve objects travel as {"__h": id, "__t": "Type"}; tables with non-string keys as {"__d": [[k, v], ...]}.
Connection details are written to ~/.resolve-pilot/bridge.json. The bridge serves until Resolve
quits or a client sends "shutdown". Re-running it while one is active does nothing.
]]

local BRIDGE_VERSION = "2.0.0"
local DEFAULT_PORT = tonumber(os.getenv("RESOLVE_PILOT_PORT") or "") or 52931

local R = resolve
if not R and bmd and bmd.scriptapp then R = bmd.scriptapp("Resolve") end
if not R then
  print("[ResolvePilot] ERROR: no resolve object. Run this inside DaVinci Resolve.")
  return
end

local ffi = ffi
if not ffi and require then
  local ok, m = pcall(require, "ffi")
  ffi = ok and m or nil
end

if not ffi then
  -- Sandboxed context (e.g. the Scripts menu on Resolve free): sockets are unavailable here, but
  -- the interactive Console allows them. Open the Console; the user (or Claude's
  -- resolve_start_bridge tool) runs the one-line dofile(...) command there.
  local fu_ = fusion or fu or R:Fusion()
  local path = fu_ and fu_:MapPath("Scripts:/Utility/ResolvePilot Bridge.lua") or "ResolvePilot Bridge.lua"
  local cmd = "dofile([[" .. path:gsub("\\", "/") .. "]])"
  print("[ResolvePilot] Open Workspace > Console (F6) and run:  " .. cmd)
  -- Custom UI windows are Studio-only, so just open the Console for the user.
  if fu_ and fu_.ShowConsole then pcall(function() fu_:ShowConsole(true) end) end
  return
end

local IS_WIN = ffi.os == "Windows"
local IS_MAC = ffi.os == "OSX"
local unpack = unpack or table.unpack

-- ---------------------------------------------------------------------------
-- C declarations (each guarded: cdef raises if a declaration already exists)
-- ---------------------------------------------------------------------------
local function cdef(s) pcall(ffi.cdef, s) end

cdef [[
typedef struct FILE FILE;
FILE *fopen(const char *path, const char *mode);
size_t fwrite(const void *p, size_t size, size_t n, FILE *f);
int fclose(FILE *f);
int remove(const char *path);
int rename(const char *a, const char *b);
]]

local S -- socket library
if IS_WIN then
  cdef [[
  typedef uint64_t RP_SOCKET;
  typedef struct { int16_t sin_family; uint16_t sin_port; uint32_t sin_addr; char sin_zero[8]; } rp_sockaddr_in;
  typedef struct { RP_SOCKET fd; int16_t events; int16_t revents; } rp_pollfd;
  int WSAStartup(uint16_t ver, void *data);
  int WSAGetLastError(void);
  RP_SOCKET socket(int af, int type, int proto);
  int bind(RP_SOCKET s, const rp_sockaddr_in *addr, int len);
  int listen(RP_SOCKET s, int backlog);
  RP_SOCKET accept(RP_SOCKET s, void *addr, int *len);
  int recv(RP_SOCKET s, char *buf, int len, int flags);
  int send(RP_SOCKET s, const char *buf, int len, int flags);
  int closesocket(RP_SOCKET s);
  int getsockname(RP_SOCKET s, rp_sockaddr_in *addr, int *len);
  int WSAPoll(rp_pollfd *fds, unsigned long n, int timeout);
  ]]
  S = ffi.load("ws2_32")
  local wsadata = ffi.new("char[1024]")
  S.WSAStartup(0x0202, wsadata)
else
  if IS_MAC then
    cdef [[ typedef struct { uint8_t sin_len; uint8_t sin_family; uint16_t sin_port; uint32_t sin_addr; char sin_zero[8]; } rp_sockaddr_in; ]]
  else
    cdef [[ typedef struct { uint16_t sin_family; uint16_t sin_port; uint32_t sin_addr; char sin_zero[8]; } rp_sockaddr_in; ]]
  end
  cdef [[
  typedef int RP_SOCKET;
  typedef struct { int fd; int16_t events; int16_t revents; } rp_pollfd;
  RP_SOCKET socket(int af, int type, int proto);
  int bind(RP_SOCKET s, const rp_sockaddr_in *addr, uint32_t len);
  int listen(RP_SOCKET s, int backlog);
  RP_SOCKET accept(RP_SOCKET s, void *addr, uint32_t *len);
  intptr_t recv(RP_SOCKET s, char *buf, size_t len, int flags);
  intptr_t send(RP_SOCKET s, const char *buf, size_t len, int flags);
  int close(int fd);
  int getsockname(RP_SOCKET s, rp_sockaddr_in *addr, uint32_t *len);
  int setsockopt(RP_SOCKET s, int level, int name, const void *val, uint32_t len);
  ]]
  if IS_MAC then cdef [[ int poll(rp_pollfd *fds, unsigned int n, int timeout); ]]
  else cdef [[ int poll(rp_pollfd *fds, unsigned long n, int timeout); ]] end
  S = ffi.C
end

local INVALID = IS_WIN and ffi.cast("RP_SOCKET", -1) or -1
local POLLIN = IS_WIN and 0x0100 or 0x0001 -- POLLRDNORM on Windows
local MSG_NOSIGNAL = (ffi.os == "Linux") and 0x4000 or 0

local function htons(p) return (p % 256) * 256 + math.floor(p / 256) end
local function closesock(s) if IS_WIN then S.closesocket(s) else S.close(s) end end
local function poll(fds, n, ms) if IS_WIN then return S.WSAPoll(fds, n, ms) else return S.poll(fds, n, ms) end end

-- ---------------------------------------------------------------------------
-- Files
-- ---------------------------------------------------------------------------
local HOME = os.getenv(IS_WIN and "USERPROFILE" or "HOME") or os.getenv("HOME") or "."
local SEP = IS_WIN and "\\" or "/"
local STATE_DIR = HOME .. SEP .. ".resolve-pilot"
local STATE_FILE = STATE_DIR .. SEP .. "bridge.json"

local function write_file(path, data)
  local f = ffi.C.fopen(path, "wb")
  if f == nil then return false end
  ffi.C.fwrite(data, 1, #data, f)
  ffi.C.fclose(f)
  return true
end

local function ensure_state_dir()
  if bmd and bmd.direxists and bmd.direxists(STATE_DIR) then return end
  if bmd and bmd.createdir then pcall(bmd.createdir, STATE_DIR) return end
  if IS_WIN then cdef [[ int _mkdir(const char *p); ]] pcall(function() ffi.C._mkdir(STATE_DIR) end)
  else cdef [[ int mkdir(const char *p, uint32_t mode); ]] pcall(function() ffi.C.mkdir(STATE_DIR, 448) end) end
end

-- ---------------------------------------------------------------------------
-- JSON
-- ---------------------------------------------------------------------------
local json = {}
local NULL = setmetatable({}, { __tostring = function() return "null" end })
local ARRAY_LEN = setmetatable({}, { __mode = "k" })

local escapes = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
local function esc_str(s)
  return '"' .. s:gsub('[%c"\\]', function(c) return escapes[c] or string.format("\\u%04x", c:byte()) end) .. '"'
end

local encode_value -- forward

local function encode_number(n)
  if n ~= n or n == math.huge or n == -math.huge then return "null" end
  if n == math.floor(n) and math.abs(n) < 2^53 then return string.format("%d", n) end
  return string.format("%.17g", n)
end

json.encode = function(v, hook, depth)
  depth = depth or 0
  if depth > 60 then return '"<max depth>"' end
  local t = type(v)
  if v == nil or v == NULL then return "null"
  elseif t == "boolean" then return v and "true" or "false"
  elseif t == "number" then return encode_number(v)
  elseif t == "string" then return esc_str(v)
  elseif t == "table" then
    -- Resolve tags API tables with a hidden "__flags" field; 0x400000 marks a list.
    local flags = rawget(v, "__flags")
    if type(flags) == "number" and math.floor(flags / 0x400000) % 2 == 1 then
      local parts = {}
      for i = 1, #v do parts[i] = json.encode(v[i], hook, depth + 1) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    local n, maxi, allstr = 0, 0, true
    for k in pairs(v) do
      if k ~= "__flags" then
        n = n + 1
        if type(k) == "number" and k >= 1 and k == math.floor(k) then
          if maxi >= 0 and k > maxi then maxi = k end
        else
          maxi = -1
        end
        if type(k) ~= "string" then allstr = false end
      end
    end
    if n == 0 then return flags ~= nil and "{}" or "[]" end
    local parts = {}
    -- Untagged 1..n tables are lists, unless Resolve tagged them as dicts (e.g. markers at frames 1..n).
    if maxi == n and flags == nil then
      for i = 1, n do parts[i] = json.encode(v[i], hook, depth + 1) end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    if not allstr then
      for k, val in pairs(v) do
        if k ~= "__flags" then
          parts[#parts + 1] = "[" .. json.encode(k, hook, depth + 1) .. "," .. json.encode(val, hook, depth + 1) .. "]"
        end
      end
      return '{"__d":[' .. table.concat(parts, ",") .. "]}"
    end
    for k, val in pairs(v) do
      if k ~= "__flags" then
        parts[#parts + 1] = esc_str(k) .. ":" .. json.encode(val, hook, depth + 1)
      end
    end
    return "{" .. table.concat(parts, ",") .. "}"
  elseif hook then
    return hook(v)
  end
  return esc_str("<" .. t .. ">")
end

local function utf8_char(cp)
  if cp < 0x80 then return string.char(cp)
  elseif cp < 0x800 then return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
  elseif cp < 0x10000 then
    return string.char(0xE0 + math.floor(cp / 0x1000), 0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
  end
  return string.char(0xF0 + math.floor(cp / 0x40000), 0x80 + math.floor(cp / 0x1000) % 0x40,
    0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
end

json.decode = function(s)
  local pos = 1
  local function ws() pos = s:find("[^ \t\r\n]", pos) or (#s + 1) end
  local function err(msg) error("JSON " .. msg .. " at " .. pos, 0) end
  local value
  local function str()
    pos = pos + 1
    local buf = {}
    while true do
      local c = s:sub(pos, pos)
      if c == "" then err("unterminated string") end
      if c == '"' then pos = pos + 1 break end
      if c == "\\" then
        local e = s:sub(pos + 1, pos + 1)
        local map = { b = "\b", f = "\f", n = "\n", r = "\r", t = "\t", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
        if map[e] then buf[#buf + 1] = map[e]; pos = pos + 2
        elseif e == "u" then
          local cp = tonumber(s:sub(pos + 2, pos + 5), 16) or err("bad \\u escape")
          pos = pos + 6
          if cp >= 0xD800 and cp <= 0xDBFF and s:sub(pos, pos + 1) == "\\u" then
            local lo = tonumber(s:sub(pos + 2, pos + 5), 16)
            if lo and lo >= 0xDC00 and lo <= 0xDFFF then
              cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
              pos = pos + 6
            end
          end
          buf[#buf + 1] = utf8_char(cp)
        else err("bad escape") end
      else
        local j = s:find('["\\]', pos) or (#s + 1)
        buf[#buf + 1] = s:sub(pos, j - 1)
        pos = j
      end
    end
    return table.concat(buf)
  end
  value = function()
    ws()
    local c = s:sub(pos, pos)
    if c == "{" then
      pos = pos + 1
      local t = {}
      ws()
      if s:sub(pos, pos) == "}" then pos = pos + 1 return t end
      while true do
        ws()
        if s:sub(pos, pos) ~= '"' then err("expected key") end
        local k = str()
        ws()
        if s:sub(pos, pos) ~= ":" then err("expected ':'") end
        pos = pos + 1
        local v = value()
        if v ~= NULL then t[k] = v end
        ws()
        local d = s:sub(pos, pos)
        pos = pos + 1
        if d == "}" then break elseif d ~= "," then err("expected ',' or '}'") end
      end
      return t
    elseif c == "[" then
      pos = pos + 1
      local t, n = {}, 0
      ws()
      if s:sub(pos, pos) == "]" then pos = pos + 1 ARRAY_LEN[t] = 0 return t end
      while true do
        n = n + 1
        local v = value()
        if v ~= NULL then t[n] = v end
        ws()
        local d = s:sub(pos, pos)
        pos = pos + 1
        if d == "]" then break elseif d ~= "," then err("expected ',' or ']'") end
      end
      ARRAY_LEN[t] = n
      return t
    elseif c == '"' then return str()
    elseif s:sub(pos, pos + 3) == "true" then pos = pos + 4 return true
    elseif s:sub(pos, pos + 4) == "false" then pos = pos + 5 return false
    elseif s:sub(pos, pos + 3) == "null" then pos = pos + 4 return NULL
    else
      local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
      if not num or num == "" then err("unexpected character '" .. c .. "'") end
      pos = pos + #num
      return tonumber(num)
    end
  end
  local v = value()
  return v
end

-- ---------------------------------------------------------------------------
-- Object handles
-- ---------------------------------------------------------------------------
local handles = { [0] = R }
local next_handle = 1
local MAX_HANDLES = 200000
local handle_count = 0

local function type_name(o)
  local s = tostring(o)
  return s:match("^([%w_]+)") or type(o)
end

local function register(o)
  local h = next_handle
  next_handle = next_handle + 1
  handles[h] = o
  handle_count = handle_count + 1
  if handle_count > MAX_HANDLES then
    local cutoff = next_handle - MAX_HANDLES / 2
    for k in pairs(handles) do
      if k ~= 0 and k < cutoff then handles[k] = nil handle_count = handle_count - 1 end
    end
  end
  return h
end

local function encode_result(v)
  return json.encode(v, function(o)
    if type(o) == "userdata" then
      return '{"__h":' .. register(o) .. ',"__t":' .. esc_str(type_name(o)) .. "}"
    end
    return esc_str("<" .. type(o) .. ">")
  end)
end

local function decode_arg(v)
  if type(v) ~= "table" then
    if v == NULL then return nil end
    return v
  end
  if v.__h ~= nil then
    local o = handles[v.__h]
    if o == nil then error("stale object handle " .. tostring(v.__h) .. " (fetch the object again)", 0) end
    return o
  end
  if v.__d ~= nil then
    local t = {}
    for _, kv in ipairs(v.__d) do t[decode_arg(kv[1])] = decode_arg(kv[2]) end
    return t
  end
  local t = {}
  for k, val in pairs(v) do t[k] = decode_arg(val) end
  return t
end

-- ---------------------------------------------------------------------------
-- Operations
-- ---------------------------------------------------------------------------
local token = (bmd and bmd.createuuid and bmd.createuuid() or tostring(os.time()) .. tostring(os.clock()))
token = token:gsub("[^%w]", "")
local running = true
local OPS = {}

local function pack(...) return { n = select("#", ...), ... } end

function OPS.hello()
  return {
    bridge_version = BRIDGE_VERSION, language = "lua", lua = _VERSION, jit = jit and jit.version or nil,
    product = R:GetProductName(), version = R:GetVersionString(), os = ffi.os,
    pid = bmd and bmd.getpid and bmd.getpid() or nil,
  }
end

function OPS.ping() return true end

function OPS.call(req)
  local obj = handles[req.h or 0]
  if obj == nil then error("stale object handle " .. tostring(req.h) .. " (fetch the object again)", 0) end
  local fn = obj[req.m]
  if fn == nil then error("method " .. tostring(req.m) .. " is not available on " .. type_name(obj) .. " in this Resolve version", 0) end
  local a = req.a or {}
  local n = ARRAY_LEN[a] or #a
  local args = {}
  for i = 1, n do args[i] = decode_arg(a[i]) end
  local r = pack(fn(obj, unpack(args, 1, n)))
  if r.n == 0 then return nil end
  if r.n == 1 then return r[1] end
  local out = {}
  for i = 1, r.n do out[i] = r[i] == nil and NULL or r[i] end
  return out
end

function OPS.attr(req)
  local obj = handles[req.h or 0]
  if obj == nil then error("stale object handle " .. tostring(req.h), 0) end
  return obj[req.n]
end

function OPS.release(req)
  for _, h in ipairs(req.hs or {}) do
    if h ~= 0 and handles[h] ~= nil then handles[h] = nil handle_count = handle_count - 1 end
  end
  return handle_count
end

function OPS.exec_lua(req)
  local out = {}
  local function cap_print(...)
    local p = pack(...)
    local parts = {}
    for i = 1, p.n do parts[i] = tostring(p[i]) end
    out[#out + 1] = table.concat(parts, "\t")
  end
  local factory, perr = loadstring("return function(args, print) " .. req.code .. "\nend", "=resolve_exec")
  if not factory then error(perr, 0) end
  local result = factory()(decode_arg(req.args or {}), cap_print)
  return { stdout = table.concat(out, "\n"), result = result }
end

function OPS.shutdown()
  running = false
  return true
end

local function handle_line(line)
  local ok, req = pcall(json.decode, line)
  if not ok or type(req) ~= "table" then
    return '{"id":null,"ok":false,"error":' .. esc_str("bad request: " .. tostring(req)) .. "}"
  end
  local id = json.encode(req.id)
  if req.token ~= token then
    return '{"id":' .. id .. ',"ok":false,"error":"PermissionError: bad token"}'
  end
  local op = OPS[req.op]
  if not op then
    return '{"id":' .. id .. ',"ok":false,"error":' .. esc_str("unknown op " .. tostring(req.op)) .. "}"
  end
  local ok2, res = pcall(op, req)
  if not ok2 then
    return '{"id":' .. id .. ',"ok":false,"error":' .. esc_str(tostring(res)) .. "}"
  end
  local ok3, enc = pcall(encode_result, res)
  if not ok3 then
    return '{"id":' .. id .. ',"ok":false,"error":' .. esc_str("encode error: " .. tostring(enc)) .. "}"
  end
  return '{"id":' .. id .. ',"ok":true,"result":' .. enc .. "}"
end

-- ---------------------------------------------------------------------------
-- Server
-- ---------------------------------------------------------------------------
local function make_listener()
  for _, port in ipairs({ DEFAULT_PORT }) do
    local s = S.socket(2, 1, 6) -- AF_INET, SOCK_STREAM, IPPROTO_TCP
    if s ~= INVALID then
      if not IS_WIN then
        local one = ffi.new("int[1]", 1)
        S.setsockopt(s, IS_MAC and 0xffff or 1, IS_MAC and 4 or 2, one, 4) -- SO_REUSEADDR
      end
      local addr = ffi.new("rp_sockaddr_in")
      if IS_MAC then addr.sin_len = ffi.sizeof(addr) end
      addr.sin_family = 2
      addr.sin_port = htons(port)
      addr.sin_addr = 0x0100007F -- 127.0.0.1 (network order on little-endian hosts)
      if S.bind(s, addr, ffi.sizeof(addr)) == 0 and S.listen(s, 8) == 0 then
        local len = ffi.new(IS_WIN and "int[1]" or "uint32_t[1]", ffi.sizeof(addr))
        S.getsockname(s, addr, len)
        return s, htons(addr.sin_port)
      end
      closesock(s)
    end
  end
  return nil
end

local function write_state(port)
  ensure_state_dir()
  local info = OPS.hello()
  info.port = port
  info.token = token
  info.started = os.time()
  local tmp = STATE_FILE .. ".tmp"
  if write_file(tmp, json.encode(info)) then
    ffi.C.remove(STATE_FILE)
    ffi.C.rename(tmp, STATE_FILE)
  end
  return info
end

local function serve()
  local listener, port = make_listener()
  if not listener then
    print("[ResolvePilot] Port " .. DEFAULT_PORT .. " is busy: the bridge is probably already running. "
      .. "(Set RESOLVE_PILOT_PORT to use another port.)")
    return
  end
  local info = write_state(port)
  print(string.format("[ResolvePilot] bridge v%s listening on 127.0.0.1:%d (%s %s). Leave this running.",
    BRIDGE_VERSION, port, tostring(info.product), tostring(info.version)))

  local clients = {} -- list of { sock = s, buf = "" }
  local recvbuf = ffi.new("char[65536]")
  local function send_all(s, data)
    local sent, total = 0, #data
    while sent < total do
      local n = tonumber(S.send(s, ffi.cast("const char *", data) + sent, total - sent, MSG_NOSIGNAL))
      if not n or n <= 0 then return false end
      sent = sent + n
    end
    return true
  end

  while running do
    local n = 1 + #clients
    local fds = ffi.new("rp_pollfd[?]", n)
    fds[0].fd = listener
    fds[0].events = POLLIN
    for i, c in ipairs(clients) do
      fds[i].fd = c.sock
      fds[i].events = POLLIN
    end
    local r = poll(fds, n, 250)
    if r > 0 then
      if fds[0].revents ~= 0 then
        local cs = S.accept(listener, nil, nil)
        if cs ~= INVALID then
          if IS_MAC then
            -- SO_NOSIGPIPE: a client vanishing mid-send must not raise SIGPIPE inside Resolve.
            S.setsockopt(cs, 0xffff, 0x1022, ffi.new("int[1]", 1), 4)
          end
          clients[#clients + 1] = { sock = cs, buf = "" }
        end
      end
      local closed = {}
      for i, c in ipairs(clients) do
        if fds[i].revents ~= 0 then
          local got = tonumber(S.recv(c.sock, recvbuf, 65536, 0))
          if not got or got <= 0 then
            closed[#closed + 1] = i
          else
            c.buf = c.buf .. ffi.string(recvbuf, got)
            while true do
              local nl = c.buf:find("\n", 1, true)
              if not nl then break end
              local line = c.buf:sub(1, nl - 1)
              c.buf = c.buf:sub(nl + 1)
              if line:find("%S") then
                if not send_all(c.sock, handle_line(line) .. "\n") then closed[#closed + 1] = i break end
              end
              if not running then break end
            end
          end
        end
      end
      for j = #closed, 1, -1 do
        closesock(clients[closed[j]].sock)
        table.remove(clients, closed[j])
      end
    elseif r < 0 and bmd and bmd.wait then
      bmd.wait(0.1)
    end
  end
  for _, c in ipairs(clients) do closesock(c.sock) end
  closesock(listener)
  ffi.C.remove(STATE_FILE)
  print("[ResolvePilot] bridge stopped")
end

local ok, e = pcall(serve)
if not ok then
  print("[ResolvePilot] bridge crashed: " .. tostring(e))
  pcall(ffi.C.remove, STATE_FILE)
end
