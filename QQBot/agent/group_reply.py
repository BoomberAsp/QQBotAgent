from dataclasses import dataclass,field

@dataclass
class GroupReply:
    title:str
    lines:list=field(default_factory=list)
    rows:list=field(default_factory=list)
    def plain(self):return ('【'+self.title+'】\n'+'\n'.join(self.lines))[:1200]
    def markdown(self):
        def clean(text):return str(text).replace('`','').replace('<','＜').replace('>','＞').replace('*','＊')
        return ('## '+clean(self.title)+'\n'+'\n'.join('- '+clean(line) for line in self.lines))[:1200]
    async def send(self,sender):
        if self.rows and hasattr(sender,'send_group_keyboard'):
            return await sender.send_group_keyboard(self.markdown(),self.rows,self.plain())
        if hasattr(sender,'send_group_markdown'):
            return await sender.send_group_markdown(self.markdown(),self.plain())
        direct=getattr(sender,'send_group_text',None)
        if direct:return await direct(self.plain())
        await sender.send_text(self.plain());return 'sent'
