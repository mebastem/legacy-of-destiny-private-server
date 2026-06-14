"""
opcodes.py — named opcode constants (subset, from public/netimpl/netdefine.txt).

Full map is parsed at runtime by luaproto; these are just the ones the server
references by name. Extend as more systems are implemented.
"""

# globalmgr (0x0001)
GBM_LOGIN_GAME   = 0x00010001  # login -> character list
GBM_LOGIN_PLAYER = 0x00010002
GBM_CREATE_PLAYER = 0x00010003
GBM_REGIST_USER  = 0x00010004  # register/login user -> then client sends GBM_LOGIN_GAME
GBM_DELETE_PLAYER = 0x00010006

# gateway (0x0005) — the client-facing app
GW_LOGIN_GATEWAY = 0x00050001  # enter world (full snapshot)
GW_PLAYER_MOVE   = 0x00050002
GW_TELEPORT      = 0x00050004  # portal teleport: req {type,teleportID,targetID} -> {ret,mapid,x,y}
GW_TELEPORT_FINISH = 0x00050005  # client signals teleport done (cm_empty)
GW_ATTACK        = 0x00050006
GW_HEARTBEAT     = 0x00050015  # 327701, empty payload, every 10s
GW_COMEBACK      = 0x00050017  # post-enter "unstuck" signal, empty

GW_EQUIP_PANEL   = 0x00050024  # equip panel query {atk,def,hp,index}
GW_OFFICE        = 0x0005002E  # military rank/office {ret,opt,level,used,max}
GW_FUNCTION_NOTICE = 0x000500C6  # feature-unlock notice {ret,noticeID}

# client push channel (0x0007 / 0x8007)
CL_SERVER_TIME   = 0x00070053  # server -> client time push {m_nTime:uint32}; keeps socket alive
CL_AOI           = 0x80070001  # server -> client AOI events (spawn/move/del entities)
