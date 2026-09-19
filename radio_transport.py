"""Subscription-specific read-only modem polling through the router's ubus API."""
import json
import subprocess


def poll(ssh, commands):
    # One ubus connection and one SSH roundtrip. Never change SIM selection.
    script = r'''local u=require("ubus")
local j=require("luci.jsonc")
local c=assert(u.connect())
local function modem()
 local x=c:call("cellular.modem","status",{}) or {}
 for _,m in ipairs(x.modems or {}) do if m.bus=="cpu" then return m end end
 return {}
end
local a=modem()
local slot=a.current_sim_slot
if slot~=1 and slot~=2 then error("No selected modem subscription") end
local rows={}
local commands=j.parse(COMMANDS)
for _,cmd in ipairs(commands) do
 local x=c:call("modem.CPU.AT","get_result_AT",{cmd=cmd,timeout=4,source_flag=0,sub_id=slot}) or {}
 if not x.channel_status or type(x.data)~="string" then error("AT channel unavailable") end
 table.insert(rows,x.data)
end
local b=modem()
c:close()
print(j.stringify({subscription=slot,subscription_after=b.current_sim_slot,
 switch_count=a.slot_switch_count,switch_count_after=b.slot_switch_count,
 switching=a.slot_switch_status~=0 or b.slot_switch_status~=0,raw=table.concat(rows,"\n")}))
'''
    # JSON string is also valid Lua literal for ASCII AT commands.
    script = script.replace('COMMANDS', json.dumps(json.dumps(commands)))
    proc = subprocess.run(ssh + ['lua -'], input=script, text=True,
                          capture_output=True, timeout=4*len(commands)+8)
    if proc.returncode:
        raise RuntimeError('Subscription-specific modem poll failed')
    result = json.loads(proc.stdout)
    if (result.get('subscription') not in (1, 2) or
            result.get('subscription') != result.get('subscription_after') or
            result.get('switch_count') != result.get('switch_count_after') or
            result.get('switching')):
        raise RuntimeError('SIM selection changed during modem poll')
    return result
