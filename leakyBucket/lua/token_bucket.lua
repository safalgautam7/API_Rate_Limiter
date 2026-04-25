local key = KEYS[1]

local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

-- get existing data
local data = redis.call("HMGET", key, "tokens", "last_refill")

local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

-- refill tokens
local elapsed = now - last_refill
tokens = math.min(capacity, tokens + (elapsed * refill_rate))

local allowed = 0

if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

-- save updated state
redis.call("HMSET", key, "tokens", tokens, "last_refill", now)
redis.call("EXPIRE", key, 3600)

return {allowed, tokens}