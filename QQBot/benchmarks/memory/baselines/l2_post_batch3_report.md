# L2 管线 benchmark 报告

生成: 2026-10-21 22:40:50  engine@2266589  model=deepseek-chat

### p1_gamer_student

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户玩游戏只玩辅助位，不碰输出位 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户每天都会玩原神日常，一天不落 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户不打排位，只玩休闲匹配 | [preference] 游戏模式 只玩休闲匹配，不打排位 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=6,age=4)] 用户把猫送回老家给爸妈养了，现在养了一只叫旺财的柴犬 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [preference] 手机系统 iPhone |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [medium(c=3,age=13)] 用户作息特别规律，每天六点早起 | [short] 用户开学后成了熬夜党，经常凌晨两点才睡 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对花生过敏，且过敏较严重 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户心脏不太好，医生嘱咐不要通宵 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的弟弟有哮喘，家里不能长时间养带毛的宠物 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户昨天和室友大吵一架，后来和好，感觉更了解彼此 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户今年暑假和好朋友去看了人生第一场演唱会，觉得特别难忘 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户昨天在游戏中第一次深渊满星 |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=6,age=2)] 用户已搬出学校宿舍，目前在校外租房住 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=6,age=2)] 用户现在离不开咖啡，每天喝两杯美式 |
| contradiction | ct3 | single_value | ✅ MERGED | [medium(c=5,age=1)] 用户是计算机专业的学生，不是会计专业 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 7 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 4 偏好 3
开销: 24 批次，83988 in / 2287 out（cached 61568），34.6s，API错误 0

### p2_working_mom

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [short] 用户每天唯一的独处时间是睡前看书 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=3,age=9)] 用户不喝奶茶，觉得太甜受不了 | [preference] 饮品 不喝奶茶，觉得太甜 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=4,age=8)] 用户全家周末固定去爬山，两年没断过 |
| evolving_fact | ef1 | evolved | ❌ CONTRADICTED | [short] 用户有一个女儿，上幼儿园，用户曾因女儿幼儿园演出感动落泪 | [medium(c=6,age=3)] 用户女儿今年九月升小学，适应得还不错 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=5,age=3)] 用户换了工作，现在改坐地铁通勤 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=5,age=2)] 用户上个月转行做保险了，此前做了十年财务工作 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对青霉素过敏 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户怀二胎时得过妊娠糖尿病，现在很注意控糖 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的小儿子有先天性心脏病，需定期复查 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户有一个女儿，上幼儿园，用户曾因女儿幼儿园演出感动落泪 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户昨天刚过结婚十周年，丈夫准备了惊喜，用户很感动 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户半夜带儿子挂急诊，儿子发烧到39度8，所幸无大碍 |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=5,age=2)] 用户不是独生女，有一个姐姐 |
| contradiction | ct2 | single_value | ❌ CONTRADICTED | [short] 用户家里养了两条狗 | [short] 用户上个月把两条狗送人了，现在家里没有宠物 |
| contradiction | ct3 | single_value | ✅ MERGED | [medium(c=5,age=1)] 用户搬到南京了，全家去年就已搬离杭州 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 9 / MEDIUM 8 / LONG 0（时钟 24），画像槽位: 兴趣 0 偏好 1
开销: 24 批次，84107 in / 2108 out（cached 65920），32.2s，API错误 0

### p3_fitness_coach

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户饮食必须高蛋白，鸡胸肉是主食 | [preference] 饮食 高蛋白，鸡胸肉是主食 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户习惯早上五点起来锻炼，认为晚上练影响睡眠 | [preference] 锻炼时间 习惯早上五点起来练，晚上练影响睡眠 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=4,age=8)] 用户滴酒不沾，备赛期更不喝 | [preference] 饮酒 滴酒不沾 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=6,age=3)] 用户已结束减脂期，进入增肌期，保持热量盈余 |
| evolving_fact | ef2 | evolved | ❌ CONTRADICTED | [short] 用户膝盖有伤，深蹲不能做大重量 | [medium(c=3,age=3)] 用户膝盖已康复，深蹲重量恢复至一百五十公斤 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=5,age=2)] 用户已从连锁健身房私教岗位辞职，自己开了私教工作室 |
| one_shot_junk | j1 | absent | ❌ LEAKED | [short] 用户今天腿酸得走路都费劲 |
| one_shot_junk | j2 | absent | ❌ LEAKED | [short] 用户的蛋白粉喝完了，准备再买 |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户海鲜过敏，吃虾蟹会起疹子 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户年轻时做过腰椎手术，硬拉必须戴护具 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的妻子是素食主义者，家里做饭要分开做两套 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户昨天第一次深蹲起两倍体重，练了八年终于做到 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户带了三年的学员昨天跑完人生第一个全马，用户比自己跑还激动 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户上个月在省赛拿了季军，差一点进国家队选拔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=8,age=2)] 用户目前负重三十公斤做引体向上 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [interest] 力量举 |
| contradiction | ct3 | single_value | ✅ MERGED | [medium(c=4,age=2)] 用户三十五岁，不是三十岁 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 9 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 3
开销: 24 批次，85336 in / 2477 out（cached 63616），35.0s，API错误 0

### p4_indie_dev

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户开发游戏只使用 Godot 引擎，不使用其他引擎 | [preference] 游戏引擎 Godot（只用 Godot，不用其他引擎） |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=5,age=4)] 用户效率最高的时段是深夜，通常在凌晨写代码 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户开发的游戏全部采用像素风格，这是其个人招牌 | [interest] 像素风游戏 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=5,age=6)] 用户开发的第二款游戏已于上个月上架，销量尚可 |
| evolving_fact | ef2 | evolved | ❌ STALE | [medium(c=3,age=14)] 用户是独立开发者，一个人全栈负责全部开发工作 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户在老家远程办公，生活在小城市，生活成本低 | [short] 用户为了谈发行搬回深圳，之前已在老家待了三年 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户有腱鞘炎，医生建议控制打字时长 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户曾被合伙人坑骗，背负违约金债务，目前正在打官司 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [medium(c=3,age=3)] 用户开发的第一款游戏亏损三万元，约占全部积蓄的三分之一 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户开发的游戏收到第一条玩家长评好评，用户看哭了并觉得值了 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户曾参加 game jam，48 小时做完一个游戏并拿了名次，自认是最疯狂的经历 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户三年前辞职做独立开发，留下工牌直接离开，没有后悔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [short] 用户的音乐是外包的，美术以前也外包过 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=5,age=5)] 用户计划改变，目前正在谈 Switch 移植 |
| contradiction | ct3 | single_value | ✅ MERGED | [medium(c=3,age=4)] 用户已改用 Linux 系统进行开发，两年多前就已切换（此前使用 Windows） |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 8 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 1
开销: 24 批次，84795 in / 2355 out（cached 65408），34.5s，API错误 0

### p5_acg_fan

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户玩 cosplay 五年了，每个月至少出一套 | [interest] cosplay |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户收手办只买正版，盗版再便宜也不碰 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户追番从不看解说剪辑，必须看原片 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=6,age=3)] 用户的本命从初音未来换成了崩坏：星穹铁道的流萤，初音仍在心里但不再是第一本命 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=6,age=3)] 用户加入了 cos 社团，现在逛漫展都跟社团一起行动 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户在学板绘，自认为画得很烂 | [medium(c=3,age=2)] 用户的板绘水平已经练出来了，现在开始接约稿赚外快 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ❌ LEAKED | [short] 用户的手办快递到了，准备去取 |
| one_shot_junk | j3 | absent | ❌ LEAKED | [short] 用户没抢到漫展门票，很生气 |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户眼角膜受过伤，不能戴隐形眼镜，cos 时只能戴框架眼镜 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户对乳胶过敏，购买道具手套时需注意材质 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户家里人不知道用户玩 cos，聊天时不要提及此事 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户上周第一次上台 cos 走秀，虽然紧张但下台后觉得很爽 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户和认识四年的网友第一次面基，聊到展馆关门也不尴尬 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户最贵的一只手办被猫摔碎，心痛到失眠三天 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=6,age=2)] 用户最近觉得国漫好看，追了好几部国漫 |
| contradiction | ct2 | single_value | ✅ MERGED | [medium(c=5,age=2)] 用户为了流萤氪了好几个648，不再是完全不氪金的白嫖党 |
| contradiction | ct3 | single_value | ❌ STALE | [short] 用户在上海读书 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 10 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 8 偏好 2
开销: 24 批次，86211 in / 2758 out（cached 63616），35.9s，API错误 0

### p6_retired_teacher

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=3,age=9)] 用户退休后每天写一小时毛笔字，练颜体 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=3,age=9)] 用户听了一辈子京剧，最爱程派 | [interest] 京剧（程派） |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=3,age=9)] 用户一点辣都不能吃，肠胃不好 | [preference] 饮食禁忌 一点辣都不能吃（肠胃不行） |
| evolving_fact | ef1 | evolved | ❌ CONTRADICTED | [short] 用户正在学用智能手机，常记不住操作步骤 | [medium(c=3,age=3)] 用户已能熟练使用智能手机，会自己网购 |
| evolving_fact | ef2 | evolved | ✅ MERGED | [medium(c=5,age=3)] 用户上个月从老家县城搬到省城儿子家住，帮忙带孙女 |
| evolving_fact | ef3 | evolved | ✅ EVOLVED | [medium(c=6,age=2)] 用户已确诊高血压，开始每天服用降压药 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对磺胺类药物过敏，年轻时曾因过敏休克过一次 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户的老伴五年前去世，目前独自居住 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户左耳佩戴助听器，与用户交流时最好使用文字 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户上个月第一批学生回来看望他，学生都已五十多岁 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户的颜体书法在县里书法比赛中获得一等奖 |
| salient_episode | se3 | canary | 🐤 CANARY-FAIL |  |
| contradiction | ct1 | single_value | ❌ CONTRADICTED | [short] 用户退休前教数学 | [medium(c=6,age=2)] 用户退休前是语文教师，教了四十年书（不是数学） |
| contradiction | ct2 | single_value | ✅ EVOLVED | [short] 用户已学会用微信，并在老年大学群里每天发早安 |
| contradiction | ct3 | single_value | ✅ EVOLVED | [medium(c=3,age=2)] 用户的孙女上小学三年级 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 11 / MEDIUM 8 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 1
开销: 24 批次，85116 in / 2170 out（cached 66304），33.4s，API错误 0

## 汇总

| persona | 计分通过 | 通过率 | canary | 泄漏(junk/rd/植入) | 批次 | in+out token |
|---|---|---|---|---|---|---|
| p1_gamer_student | 20/21 | 95% | 3/3 | 0 | 24 | 86275 |
| p2_working_mom | 19/21 | 90% | 3/3 | 0 | 24 | 86215 |
| p3_fitness_coach | 18/21 | 86% | 3/3 | 2 | 24 | 87813 |
| p4_indie_dev | 19/21 | 90% | 3/3 | 0 | 24 | 87150 |
| p5_acg_fan | 17/21 | 81% | 3/3 | 2 | 24 | 88969 |
| p6_retired_teacher | 19/21 | 90% | 2/3 | 0 | 24 | 87286 |
| **合计** | **112/126** | **89%** | **17/18** | **4** | | |