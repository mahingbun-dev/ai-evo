"""并发池 Lua 脚本：申请/释放/续约/清理，保证多进程下的原子性"""

# KEYS: leases_zset, lease_data_hash, wait_zset
# ARGV: now_ms, ttl_ms, limit, lease_id, waiter_id, lease_json, waiter_stale_before_ms,
#       wait_alive_key_prefix
# 返回: {acquired, active, expired_detail_list, queue_position_or_-1}
ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local lease_id = ARGV[4]
local waiter_id = ARGV[5]
local lease_json = ARGV[6]
local stale_before = tonumber(ARGV[7])
local alive_prefix = ARGV[8]

local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
local expired_details = {}
for i = 1, #expired do
  local id = expired[i]
  local d = redis.call('HGET', KEYS[2], id)
  if d then table.insert(expired_details, d) end
  redis.call('HDEL', KEYS[2], id)
  redis.call('ZREM', KEYS[1], id)
end

redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', stale_before)

-- 幽灵队首回收：等待者被 SIGKILL / 硬超时杀掉后无法 ZREM 自己，它的存活凭证
-- 也不再刷新。凭证缺失即判死并出队；否则即使 active=0，am_head=false 也会把
-- 所有后来者挡到 wait_timeout（900s）过期为止
local head = redis.call('ZRANGE', KEYS[3], 0, 0)
while #head > 0 and redis.call('EXISTS', alive_prefix .. head[1]) == 0 do
  redis.call('ZREM', KEYS[3], head[1])
  head = redis.call('ZRANGE', KEYS[3], 0, 0)
end

local active = redis.call('ZCARD', KEYS[1])
local am_head = (#head == 0) or (head[1] == waiter_id)

if active < limit and am_head then
  redis.call('ZADD', KEYS[1], now + ttl, lease_id)
  redis.call('HSET', KEYS[2], lease_id, lease_json)
  redis.call('ZREM', KEYS[3], waiter_id)
  redis.call('DEL', alive_prefix .. waiter_id)
  return {1, active + 1, expired_details, -1}
end

redis.call('ZADD', KEYS[3], 'NX', now, waiter_id)
local rank = redis.call('ZRANK', KEYS[3], waiter_id)
if rank == false then rank = -1 end
return {0, active, expired_details, rank}
"""

# KEYS: leases_zset, lease_data_hash   ARGV: lease_id
# 返回: {removed, detail_or_empty}
RELEASE_SCRIPT = """
local detail = redis.call('HGET', KEYS[2], ARGV[1])
local removed = redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
if detail == false then detail = '' end
return {removed, detail}
"""

# KEYS: leases_zset, lease_data_hash   ARGV: lease_id, new_expire_ms, lease_json
RENEW_SCRIPT = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
  redis.call('HSET', KEYS[2], ARGV[1], ARGV[3])
  return 1
end
return 0
"""

# KEYS: leases_zset, lease_data_hash, wait_zset
# ARGV: now_ms, waiter_stale_before_ms
# 返回: {expired_detail_list, stale_waiter_count}
PRUNE_SCRIPT = """
local now = tonumber(ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
local details = {}
for i = 1, #expired do
  local id = expired[i]
  local d = redis.call('HGET', KEYS[2], id)
  if d then table.insert(details, d) end
  redis.call('HDEL', KEYS[2], id)
  redis.call('ZREM', KEYS[1], id)
end
local stale = redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', tonumber(ARGV[2]))
return {details, stale}
"""
