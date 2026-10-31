# L2 管线 benchmark 报告

生成: 2026-10-21 00:23:10  engine@0c6e39b  model=deepseek-chat

### p1_gamer_student

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户玩游戏只玩辅助位，从不玩输出位 | [interest] 游戏辅助位 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户每天都做原神日常，一天不落 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户打游戏只玩休闲匹配，不打排位 | [preference] 游戏模式偏好 只玩休闲匹配，不打排位 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=6,age=4)] 用户把猫送回老家给爸妈养了，现在养了一只叫旺财的柴犬 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [preference] 游戏设备 iPhone |
| evolving_fact | ef3 | evolved | ✅ EVOLVED | [medium(c=5,age=2)] 用户开学后变成熬夜党，经常凌晨两点才睡 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对花生严重过敏 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户心脏不太好，医生嘱咐不要通宵 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的弟弟有哮喘，家里不能长时间养带毛的宠物 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户昨天和室友大吵了一架，后来和好，感觉反而更了解彼此了 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户暑假和好朋友去看了人生第一场演唱会，觉得特别难忘 | [interest] 看演唱会 |
| salient_episode | se3 | canary | 🐤 CANARY-FAIL |  |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=6,age=2)] 用户已搬出学校宿舍，现在在校外租房住 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=6,age=2)] 用户现在每天喝两杯美式咖啡，离不开咖啡 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户是会计专业的学生 | [medium(c=3,age=1)] 用户是计算机专业的学生（此前误说成会计专业） |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 6 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 5 偏好 3
开销: 24 批次，47142 in / 2075 out（cached 28288），26.8s，API错误 0

### p2_working_mom

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ❌ MISSING |  |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户不喝奶茶，觉得太甜受不了 | [preference] 饮品偏好 不喝奶茶，觉得太甜受不了 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户全家周末固定去爬山，已坚持两年 |
| evolving_fact | ef1 | evolved | ❌ CONTRADICTED | [short] 用户的女儿在上幼儿园，用户曾因女儿幼儿园演出感动落泪 | [medium(c=4,age=14)] 用户的女儿刚上幼儿园，天天哭鼻子 |
| evolving_fact | ef2 | evolved | ❌ CONTRADICTED | [medium(c=3,age=14)] 用户每天开车通勤，单程四十分钟 | [short] 用户换工作了，现在改坐地铁通勤 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=6,age=2)] 用户上个月从做了十年的财务工作转行做保险 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ❌ LEAKED | [short] 用户有孩子，需要查询儿童感冒药的用量说明 |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对青霉素过敏 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户怀二胎时得过妊娠糖尿病，现在很注意控糖 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的小儿子有先天性心脏病，需要定期复查 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户的女儿在上幼儿园，用户曾因女儿幼儿园演出感动落泪 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户已婚，昨天刚过结婚十周年，丈夫为她准备了惊喜 |
| salient_episode | se3 | canary | 🐤 CANARY-FAIL |  |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=8,age=2)] 用户有个姐姐，不是独生女 |
| contradiction | ct2 | single_value | ✅ MERGED | [medium(c=7,age=2)] 用户家里之前养的两条狗上个月送人了，现在没有宠物 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户已不住在杭州 | [short] 用户住在杭州 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 9 / MEDIUM 10 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 1
开销: 24 批次，47112 in / 2233 out（cached 28672），28.8s，API错误 0

### p3_fitness_coach

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户饮食上必须高蛋白，把鸡胸肉当主食 | [preference] 饮食偏好 高蛋白，鸡胸肉为主食 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户习惯早上五点起床锻炼，认为晚上锻炼影响睡眠 | [preference] 锻炼时间偏好 习惯早上五点起床锻炼，晚上锻炼影响睡眠 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户滴酒不沾，备赛期尤其不喝酒 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=7,age=3)] 用户减脂期结束，现在转入增肌期，采用热量盈余 |
| evolving_fact | ef2 | evolved | ❌ CONTRADICTED | [medium(c=3,age=14)] 用户膝盖有伤，深蹲不能做大重量 | [medium(c=3,age=3)] 用户膝盖已康复，深蹲重量重回150 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=6,age=2)] 用户已从连锁健身房私教岗位辞职，自己开了一家私教工作室 |
| one_shot_junk | j1 | absent | ❌ LEAKED | [medium(c=5,age=8)] 用户今天腿酸得不行，走路都费劲 |
| one_shot_junk | j2 | absent | ❌ LEAKED | [medium(c=4,age=8)] 用户的蛋白粉喝完了，需要再买 |
| one_shot_junk | j3 | absent | ❌ LEAKED | [medium(c=5,age=6)] 用户居住或健身场所的跑步机坏了，正等物业维修 |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ❌ LEAKED | [short] 用户准备报名参加省健美公开赛 |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对海鲜过敏，吃虾蟹会起疹子 | [preference] 饮食禁忌 海鲜过敏（虾蟹），会起疹子 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户年轻时做过腰椎手术，硬拉必须戴护具 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的妻子是素食主义者，家里做饭要分两套 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户练了八年健身，昨天第一次深蹲起两倍体重 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户带了三年的学员昨天跑完了人生第一个全马 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户上个月在省赛拿了季军，差一点进国家队选拔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=7,age=2)] 用户做引体向上负重30公斤 |
| contradiction | ct2 | single_value | ✅ MERGED | [medium(c=6,age=2)] 用户现在专攻力量举，拳击训练已停止 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户今年三十岁 | [medium(c=3,age=1)] 用户今年三十五岁 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 9 / MEDIUM 13 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 3
开销: 24 批次，52387 in / 2901 out（cached 29056），30.3s，API错误 0

### p4_indie_dev

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户做游戏只使用 Godot 引擎，不用其他引擎 | [preference] 游戏引擎 Godot |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户效率最高的时段是深夜，代码通常在凌晨写 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户的游戏全部采用像素风，这是其招牌风格 | [interest] 像素风游戏 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=5,age=4)] 用户的第二款游戏已于上个月上架，销量尚可 |
| evolving_fact | ef2 | evolved | ❌ STALE | [short] 用户是独立开发者，一个人做全栈全包开发 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户在老家远程办公，所在小城市生活成本较低 | [short] 用户为了谈发行搬回深圳，此前在老家待了三年 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户患有腱鞘炎，医生建议控制打字时长 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户曾被合伙人坑过，欠下违约金，目前正在打官司 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的第一款游戏亏损三万元，相当于其全部积蓄的三分之一 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户的游戏收到了第一条玩家长评好评，用户看哭了 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户去年参加 game jam，48 小时做完一个游戏并拿了名次 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户三年前辞职做独立开发，把工牌留在工位上直接离开，至今不后悔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=6,age=4)] 用户的游戏音乐是外包的，美术以前也外包过 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=6,age=4)] 用户计划有变，正在洽谈将游戏移植到 Switch 平台 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户使用 Windows 系统开发游戏 | [short] 用户两年多前改用 Linux 系统开发游戏 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 12 / MEDIUM 6 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 2
开销: 24 批次，48652 in / 1969 out（cached 30720），25.9s，API错误 0

### p5_acg_fan

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户玩 cosplay 已经五年，每个月至少出一套 | [interest] cosplay |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户收藏手办只买正版，不买盗版 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户追番从不看解说剪辑，必须看原片 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=7,age=3)] 用户的本命换成了崩坏：星穹铁道的流萤，初音未来仍是心里的重要角色但本命顺位变了 |
| evolving_fact | ef2 | evolved | ✅ MERGED | [medium(c=5,age=3)] 用户加入了 cos 社团，现在逛漫展都跟社团一起，不再是一个人 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户正在学板绘，自称画得很烂 | [medium(c=3,age=2)] 用户板绘水平已提升到可以接约稿赚外快 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户眼角膜受过伤，不能戴隐形眼镜，出 cos 只能戴框架改 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户对乳胶过敏，买道具手套时需特别注意材质 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户家里人不知道其玩 cos，聊天时需要回避这个话题 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户上周第一次上台 cos 走秀，紧张到腿抖但下台后特别兴奋 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户和认识四年的网友第一次面基，聊到展馆关门都不觉得尴尬 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户最贵的一只手办被猫推下去摔碎了，心痛到失眠三天 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [preference] 观看偏好 现在也看国漫，追了好几部 |
| contradiction | ct2 | single_value | ❌ CONTRADICTED | [preference] 消费习惯 玩游戏从不氪金，是白嫖党 | [medium(c=6,age=2)] 用户为了崩坏：星穹铁道的流萤氪金了好几个648，不再坚持白嫖 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户在上海读书 | [short] 用户已经毕业，现在回到成都生活，上海是上学时待过的地方 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 9 / MEDIUM 8 / LONG 0（时钟 24），画像槽位: 兴趣 13 偏好 2
开销: 24 批次，49652 in / 2485 out（cached 28800），27.7s，API错误 0

### p6_retired_teacher

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户退休后每天写一小时毛笔字，练的是颜体 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户听京剧听了一辈子，最爱程派 | [interest] 京剧 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户一点辣都不能吃，肠胃不行 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=6,age=3)] 用户手机用熟练了，现在会自己网购 |
| evolving_fact | ef2 | evolved | ❌ CONTRADICTED | [short] 用户一个人住在老家县城 | [medium(c=3,age=3)] 用户上个月搬到省城儿子家住，帮忙带孙女 |
| evolving_fact | ef3 | evolved | ✅ EVOLVED | [medium(c=6,age=2)] 用户已确诊高血压，开始每天吃降压药 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对磺胺类药物过敏，年轻时曾因此休克过一次 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户去年与老伴庆祝金婚纪念，为老伴写了一幅长卷，老伴去世前看到了 | [short] 用户的老伴五年前去世，目前独自一人生活 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户左耳佩戴助听器，与人交流时更希望用文字 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户教了三十年书，上个月第一批学生（都已五十多岁）回来看望他 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户的颜体书法在县里书法比赛中获得一等奖 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户去年与老伴庆祝金婚纪念，为老伴写了一幅长卷，老伴去世前看到了 |
| contradiction | ct1 | single_value | ❌ CONTRADICTED | [short] 用户退休前是数学教师，教了三十年书 | [short] 用户退休前是数学教师 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [short] 用户现在使用微信，并在老年大学群里每天发早安 | [medium(c=5,age=2)] 用户现在开始使用微信，并在老年大学群里每天发早安 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户的孙女在上初中 | [short] 用户的孙女在上小学三年级 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 14 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 1
开销: 24 批次，48730 in / 2275 out（cached 29312），29.9s，API错误 0

## 汇总

| persona | 计分通过 | 通过率 | canary | 泄漏(junk/rd/植入) | 批次 | in+out token |
|---|---|---|---|---|---|---|
| p1_gamer_student | 20/21 | 95% | 2/3 | 0 | 24 | 49217 |
| p2_working_mom | 16/21 | 76% | 2/3 | 1 | 24 | 49345 |
| p3_fitness_coach | 15/21 | 71% | 3/3 | 4 | 24 | 55288 |
| p4_indie_dev | 18/21 | 86% | 3/3 | 0 | 24 | 50621 |
| p5_acg_fan | 18/21 | 86% | 3/3 | 0 | 24 | 52137 |
| p6_retired_teacher | 18/21 | 86% | 3/3 | 0 | 24 | 51005 |
| **合计** | **105/126** | **83%** | **16/18** | **5** | | |