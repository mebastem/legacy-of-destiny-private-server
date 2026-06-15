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
GW_PICKUP        = 0x00050007  # pick up a dropped item: req {m_nDropbagID}
GW_TASK_OP       = 0x00050012  # quest accept/submit: req {m_nOpType(1=accept,2=submit), m_nTaskId}
GW_HEARTBEAT     = 0x00050015  # 327701, empty payload, every 10s
GW_COMEBACK      = 0x00050017  # post-enter "unstuck" signal, empty

GW_QUERY_RANKING = 0x00050027  # query a leaderboard: req {m_strRankingName, m_nPage} -> ranked list
GW_EQUIP_INFO    = 0x0005001D  # equip enhancement state (strong/refine/gem/magic/polish/star), empty req
GW_EQUIP_WEAR    = 0x0005001E  # equip/unequip: req {m_nOpt(1=wear,2=remove), m_nUniqueID}
GW_EQUIP_PANEL   = 0x00050024  # equip panel query {atk,def,hp,index}
GW_OFFICE        = 0x0005002E  # military rank/office {ret,opt,level,used,max}
GW_FUNCTION_NOTICE = 0x000500C6  # feature-unlock notice {ret,noticeID}

# client push channel (0x0007 / 0x8007)
CL_SERVER_TIME   = 0x00070053  # server -> client time push {m_nTime:uint32}; keeps socket alive
CL_AOI           = 0x80070001  # server -> client AOI events (spawn/move/del entities)
CL_PLAYER_MONEY  = 0x80070002  # server -> client wallet {m_vecMoney:[type,amount,...]}
CL_PLAYER_ITEMS  = 0x80070003  # server -> client full inventory (init the bag)
CL_UPDATE_INFO   = 0x80070004  # server -> client info updates (exp/level/...) {m_vecInfo:[type,val,...]}
CL_UPDATE_ITEMS  = 0x80070006  # server -> client item add/del/update
CL_UPDATE_TASK   = 0x80070007  # server -> client quest changes {m_vecUpdateInfo:[{taskId,updType,value}]}
CL_PLAYER_TASKS  = 0x80070008  # server -> client the player's quests {m_vecTask:[{taskId,state,..}]}
