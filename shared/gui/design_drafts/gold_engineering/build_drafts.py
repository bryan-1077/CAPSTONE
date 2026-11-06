"""Review-only vector mockups. Does not import or change the working GUI."""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STYLES = {
    'a': dict(name='Blueprint + Brass', tagline='Familiar blue. Intentional gold. Clear hierarchy.',
              bg='#18334b', panel='#172d40', inset='#102535', raised='#223f54', border='#3d6078',
              text='#edf3f7', muted='#adc2d0', gold='#d6b464', goldwash='#3e3a2b', radius=7),
    'b': dict(name='Precision Console', tagline='A precise, compact control-room instrument.',
              bg='#142638', panel='#1b3144', inset='#10202e', raised='#284358', border='#456074',
              text='#f0f4f7', muted='#b0c4d0', gold='#dfbb67', goldwash='#40392a', radius=2),
    'c': dict(name='Engineering Studio', tagline='Softer slate blue. Rounded panels. Warm brass.',
              bg='#233e54', panel='#2a465d', inset='#1c3448', raised='#35536a', border='#567286',
              text='#f4f5f4', muted='#c0cfda', gold='#e2bf78', goldwash='#4c4537', radius=13),
}


class Draft:
    def __init__(self, style, screen):
        self.c = STYLES[style]
        self.style, self.screen = style, screen
        self.parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000" role="img" aria-label="'+escape(self.c['name']+' '+screen+' design mockup')+'">']
        self.box(0, 0, 1600, 1000, self.c['bg'], radius=0)

    def box(self, x, y, w, h, fill=None, stroke=None, radius=None, width=1):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{self.c["radius"] if radius is None else radius}" fill="{fill or self.c["panel"]}" stroke="{stroke or "none"}" stroke-width="{width}"/>')

    def text(self, x, y, value, size=16, color=None, weight=400, anchor='start', mono=False, tracking=0):
        family = 'JetBrains Mono,DejaVu Sans Mono,monospace' if mono else 'Red Hat Text,Noto Sans,Segoe UI,sans-serif'
        self.parts.append(f'<text x="{x}" y="{y}" fill="{color or self.c["text"]}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" font-family="{family}" letter-spacing="{tracking}">{escape(str(value))}</text>')

    def line(self, x1, y1, x2, y2, color=None, width=1, opacity=1):
        self.parts.append(f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="{color or self.c["border"]}" stroke-width="{width}" opacity="{opacity}"/>')

    def path(self, path, color=None, width=1, opacity=1):
        self.parts.append(f'<path d="{path}" fill="none" stroke="{color or self.c["gold"]}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round" opacity="{opacity}"/>')

    def dot(self, x, y, r=3, color=None, filled=False):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" stroke="{color or self.c["gold"]}" fill="{(color or self.c["gold"]) if filled else self.c["bg"]}" stroke-width="1.2"/>')

    def button(self, x, y, w, label, primary=False, danger=False, h=40):
        fill = self.c['gold'] if primary else self.c['inset']
        border = '#ab6972' if danger else self.c['gold'] if primary else self.c['border']
        self.box(x, y, w, h, fill, border)
        self.text(x+w/2, y+h/2+5, label, 14, '#162b3b' if primary else '#efb0b8' if danger else self.c['text'], 650, 'middle')

    def badge(self, x, y, label, status, w=100):
        fg, bg = {'running': ('#85dbea','#1a4859'), 'passed': ('#a1dfb8','#214c3c'),
                  'waiting': ('#ffd7a0','#624126'), 'neutral': (self.c['muted'],self.c['raised']),
                  'failed': ('#ffc1c8','#56313e')}[status]
        self.box(x,y,w,28,bg,radius=5)
        self.text(x+w/2,y+19,label,12,fg,650,'middle')

    def status_icon(self, x, y, status):
        color={'passed':'#9bdab0','running':'#85dbea','waiting':'#ffc47e','neutral':self.c['muted']}[status]
        self.dot(x,y,8,color)
        if status=='passed': self.path(f'M{x-4} {y} l3 3 l6 -6',color,1.7)
        elif status=='running':
            self.path(f'M{x-2} {y-4} l5 4 l-5 4 Z',color,1.5)
        elif status=='waiting':
            self.line(x-2,y-3,x-2,y+3,color,1.6);self.line(x+2,y-3,x+2,y+3,color,1.6)
        else:self.dot(x,y,1,color,True)

    def chip(self,x,y,waiting=False):
        self.box(x+12,y+6,42,42,self.c['inset'],self.c['gold'],5,1.3)
        self.box(x+21,y+15,24,24,self.c['raised'],self.c['border'],3)
        for n in range(5):
            u=16+n*8
            self.line(x+u,y,x+u,y+6,self.c['gold'],2)
            self.line(x+u,y+48,x+u,y+54,self.c['gold'],2)
            self.line(x+6,y+u-8,x+12,y+u-8,self.c['gold'],2)
            self.line(x+54,y+u-8,x+60,y+u-8,self.c['gold'],2)
        if not waiting:
            self.path(f'M{x-4} {y+18} l-5 6 l8 1 l-5 7','#85dbea',1.6)
            self.path(f'M{x+68} {y+10} l-5 7 l7 2 l-4 7','#85dbea',1.6)

    def header(self):
        c=self.c
        if self.style=='b':
            self.box(0,0,1600,8,c['gold'],radius=0)
            self.text(32,54,'MC2 / DESIGN CONSOLE',11,c['gold'],650,tracking=1.5)
        self.text(800,53,'Ramageddon - LLM MC2',28,c['text'],700,'middle',tracking=.8)
        self.text(800,82,'Agentic AI DDR4 Memory Controller Chip Design Flow',13,c['muted'],400,'middle')
        self.button(1260,35,137,'New Design',primary=True)
        self.button(1411,35,157,'Saved Runs')
        if self.style=='a':
            for off in (0,8,16):
                self.path(f'M32 {109+off} H300 l22 -12 H500',c['border'],1)
                self.path(f'M1568 {109+off} H1300 l-22 -12 H1100',c['border'],1)
                self.dot(500,97+off,2,c['gold'])
                self.dot(1100,97+off,2,c['gold'])
            for x in range(770,832,12):self.box(x,109,5,13,c['gold'],radius=0)
        elif self.style=='b':
            self.line(32,111,1568,111,c['border'])
            for x in range(32,220,16):self.line(x,108,x+6,108,c['gold'],2)
        else:
            self.line(32,112,1568,112,c['border'])
            self.box(32,109,110,5,c['gold'],radius=2)
            self.box(1458,109,110,5,c['gold'],radius=2)
        self.text(32,151,'SELECTED RUN',10,c['muted'],650,tracking=1.2)
        self.text(150,151,'gui_e68b330b4907421ea576b17606426a16',12,c['muted'],mono=True)
        self.text(32,185,'DDR4  /  4 Banks  /  200 MHz Target',22,c['text'],650)
        if self.screen=='approval':self.badge(1028,156,'Waiting for You','waiting',154)
        elif self.screen=='results':self.badge(1080,156,'Failed','failed',102)
        else:self.badge(1080,156,'Running','running',102)
        # Navigation has one quiet continuous baseline rather than tab-shaped boxes.
        self.line(32,247,1182,247,c['border'])
        for i,name in enumerate(('Run','Results','Files')):
            x=32+i*108;active=name==('Files' if self.screen=='files' else 'Results' if self.screen=='results' else 'Run')
            if self.style=='c' and active:self.box(x-1,207,90,35,c['raised'],radius=7)
            self.text(x+44,231,name,15,c['gold'] if active else c['muted'],650 if active else 450,'middle')
            if active:self.line(x+5,247,x+83,247,c['gold'],3)
        self.text(1182,231,'Runs Directory: shared/top_orchestrator/runs',11,c['muted'],anchor='end')

    def saved_runs(self):
        c=self.c
        self.box(1206,207,362,718,c['panel'],c['border'])
        self.text(1230,242,'Saved Runs',18,c['text'],650)
        self.text(1544,241,'···',22,c['muted'],anchor='end')
        self.line(1206,263,1568,263,c['border'])
        cards=[('gui_e68b330b…','DDR4 · 4 banks · 200 MHz','Waiting for You' if self.screen=='approval' else 'Failed' if self.screen=='results' else 'Running','waiting' if self.screen=='approval' else 'failed' if self.screen=='results' else 'running','Today, 10:42'),
               ('gui_6a0d6eee…','DDR4 · 4 banks · 200 MHz','Connection Error','failed','Yesterday'),
               ('20261013T215401Z_2782d77a','DDR4 · 4 banks · 200 MHz','Complete','passed','Oct 13, 17:08')]
        for i,(name,hardware,status,color,date) in enumerate(cards):
            y=281+i*155
            self.box(1222,y,330,137,c['raised'] if i==0 else c['panel'],c['gold'] if i==0 else c['border'])
            if i==0:
                self.box(1222,y+10,3,117,c['gold'],radius=1)
                self.text(1535,y+25,'Selected',11,c['gold'],650,'end')
            self.text(1238,y+29,name,11,c['text'],550,mono=True)
            self.text(1238,y+61,hardware,13,c['muted'])
            self.badge(1238,y+82,status,color,132 if color in ('failed','waiting') else 85)
            self.text(1536,y+111,date,10,c['muted'],anchor='end')
        self.text(1230,801,'Choose a saved run to inspect its',12,c['muted'])
        self.text(1230,821,'stages, results, and files.',12,c['muted'])
        self.line(1230,855,1544,855,c['border'])
        self.text(1230,885,'All tabs follow the selected run.',11,c['muted'])

    def flow(self):
        c=self.c;waiting=self.screen=='approval'
        self.box(32,267,1150,230,c['panel'],c['border'])
        self.text(56,301,'Design Flow',18,c['text'],650)
        self.text(1158,299,'APPROVAL REQUIRED' if waiting else 'STEP 02 / 04',10,'#ffc47e' if waiting else c['gold'],650,'end',tracking=1)
        captions=['RTL Generation','Verification','Physical Design','GDSII Validation']
        statuses=['Waiting' if waiting else 'Passed','Pending' if waiting else 'Running','Pending','Pending']
        for i,(name,status) in enumerate(zip(captions,statuses)):
            x=56+i*278;state='waiting' if waiting and i==0 else 'passed' if i==0 else 'running' if i==1 and not waiting else 'neutral'
            selected=i==(0 if waiting else 1)
            self.box(x,323,254,84,c['raised'] if selected else c['inset'],c['gold'] if selected else c['border'])
            if self.style=='b':self.text(x+13,343,f'0{i+1}',10,c['gold'],650,mono=True)
            self.text(x+127,353,name,15,c['text'],650,'middle')
            self.status_icon(x+82,378,state)
            self.text(x+101,383,status,13,{'passed':'#9bdab0','running':'#85dbea','waiting':'#ffc47e'}.get(state,c['muted']),550)
            if i<3:self.path(f'M{x+257} 365 h16 l-4 -4 m4 4 l-4 4',c['border'],1.3)
        self.button(56,438,150,'Stop Run',danger=True)
        self.chip(238,431,waiting)
        self.text(326,456,'Paused for Your Approval' if waiting else 'Functional Verification Running',17,'#ffc47e' if waiting else c['text'],650)
        self.text(326,477,'Progress resumes after you respond.' if waiting else 'Agent 2 is executing the generated UVM tests.',12,c['muted'])
        self.text(1158,454,'ELAPSED',10,c['muted'],550,'end',tracking=1)
        self.text(1158,477,'03m 42s',16,c['text'],500,'end',mono=True)
        self.box(56,489,1102,3,c['inset'],radius=1)
        self.box(56,489,310,3,c['border'] if waiting else '#79cedf',radius=1)

    def running(self):
        c=self.c;self.flow()
        self.text(32,538,'Verification',21,c['text'],650)
        self.button(820,513,204,'Approved Specification')
        self.button(1038,513,144,'Hide Run Log')
        self.box(32,560,650,365,c['panel'],c['border'])
        self.text(56,595,'Verification Agents',17,c['text'],650)
        self.text(656,593,'1 complete · 1 active · 1 pending',11,c['muted'],anchor='end')
        self.line(56,613,658,613,c['border'])
        self.status_icon(68,645,'passed');self.text(89,650,'Agent 1  Structural Validation',15,c['text'],600)
        self.badge(556,630,'Passed','passed',100)
        self.line(56,676,658,676,c['border'])
        self.box(48,688,618,148,c['inset'],radius=5)
        self.box(48,700,3,124,'#85dbea',radius=1)
        self.status_icon(68,718,'running');self.text(89,723,'Agent 2  Functional Verification',15,c['text'],650)
        self.badge(556,703,'Running','running',100)
        self.text(89,758,'Generates and runs functional tests for the controller.',13,c['muted'])
        self.text(89,788,'Current task',12,c['muted']);self.text(203,788,'Executing UVM simulation',13,c['text'])
        self.text(89,814,'Final results and coverage are reported when the agent finishes.',12,c['muted'])
        self.status_icon(68,875,'neutral');self.text(89,880,'Agent 3  Protocol and Timing',15,c['text'],550)
        self.badge(556,860,'Pending','neutral',100)
        self.box(698,560,484,365,c['inset'],c['border'])
        self.text(722,595,'Run Log',16,c['text'],650)
        self.badge(1054,575,'Following','running',104)
        self.line(698,613,1182,613,c['border'])
        lines=[('17:04:22','Agent 1 completed: PASS'),('17:04:23','Starting Agent 2'),('17:04:25','Generating system testbench'),('17:04:31','Compiling controller RTL'),('17:04:48','UVM simulation started'),('17:04:49','Running traffic scenarios…')]
        for i,(time,message) in enumerate(lines):
            self.text(722,647+i*37,time,11,c['muted'],mono=True)
            self.text(796,647+i*37,message,12,'#85dbea' if i==5 else c['text'],mono=True)
        self.text(722,900,'Scroll to inspect output · Jump to Latest resumes following',11,c['muted'])

    def approval(self):
        c=self.c;self.flow()
        self.box(32,521,1150,404,c['panel'],c['border'])
        self.box(32,533,4,380,'#ffc47e',radius=1)
        self.badge(56,545,'Waiting for You','waiting',147)
        self.text(56,608,'Review Your Design Specification',25,c['text'],650)
        self.text(56,638,'Confirm the proposed settings before RTL generation begins.',15,c['muted'])
        self.button(56,662,176,'Approve Design',primary=True,h=44)
        self.button(246,662,196,'Decline and Stop',danger=True,h=44)
        self.text(1156,691,'Your run will remain paused until you respond.',12,c['muted'],anchor='end')
        self.line(56,731,1158,731,c['border'])
        for i,(name,value,detail) in enumerate([('MEMORY','DDR4','Controller protocol'),('TOPOLOGY','4 Banks','Requested bank count'),('TARGET CLOCK','200 MHz','Target; not measured timing')]):
            x=56+i*370
            self.text(x,766,name,11,c['gold'],650,tracking=.7)
            self.box(x,779,330,36,c['inset'],c['border'],5)
            self.text(x+12,804,value,20,c['text'],650)
            self.text(x+311,803,'⌄',15,c['muted'],anchor='end')
            self.text(x,828,detail,13,c['muted'])
        self.text(56,877,'Assumptions',14,c['text'],650)
        self.text(180,877,'Default timing profile applied. Review the full specification before approving.',13,c['muted'])
        self.text(56,904,'Adjust selections above · View Full Specification  →',13,c['gold'],600)

    def results(self):
        c=self.c
        self.text(32,299,'Design Results',23,c['text'],650)
        self.button(978,269,204,'Approved Specification')
        self.box(32,322,1150,147,c['panel'],c['border'])
        self.box(32,334,4,123,'#efb0b8',radius=1)
        self.badge(56,341,'Physical Design Stopped','failed',187)
        self.text(56,400,'RTL preparation timed out before synthesis could begin.',19,c['text'],650)
        self.text(56,434,'Verification evidence is available. A completed physical layout was not produced.',14,c['muted'])
        self.button(975,340,183,'Open Stage Details')
        self.button(975,397,183,'View Evidence')
        for i,(title,value,detail) in enumerate([('REQUIREMENTS','15 / 15','Passed in the verification report'),('TEST VECTORS','29 Passed','1 additional vector not applicable'),('COVERAGE LIMITATIONS','11 Reported','Review the documented gaps')]):
            x=32+i*390
            self.box(x,491,370,124,c['panel'],c['border'])
            self.text(x+24,520,title,11,c['gold'],650,tracking=.6)
            self.text(x+24,560,value,27,c['text'],650)
            self.text(x+24,592,detail,13,c['muted'])
        self.box(32,637,562,288,c['panel'],c['border'])
        self.text(56,675,'Verification Scope',18,c['text'],650)
        for i,(name,status,kind) in enumerate([('Functional RTL','Passed','passed'),('Protocol and Timing','With Gaps','waiting'),('Physical Timing','Not Verified','neutral')]):
            y=705+i*53
            self.text(56,y+20,name,14,c['text'])
            self.badge(434,y,status,kind,135)
        self.text(56,898,'View Coverage Limitations  →',13,c['gold'],600)
        self.box(616,637,566,288,c['panel'],c['border'])
        self.text(640,675,'Deliverables',18,c['text'],650)
        self.text(640,725,'Published RTL Snapshot',16,c['text'],650)
        self.text(640,753,'Immutable snapshot used by verification.',13,c['muted'])
        self.button(1005,708,153,'Browse Files')
        self.line(640,781,1158,781,c['border'])
        self.text(640,818,'GDSII Layout',16,c['text'],650)
        self.badge(1016,798,'Not Produced','neutral',142)
        self.text(640,852,'Timing, area, and power measurements are unavailable.',13,c['muted'])
        self.text(640,898,'200 MHz is the requested target, not measured performance.',12,c['muted'])

    def files(self):
        c=self.c
        self.box(32,267,1150,658,c['panel'],c['border'])
        self.box(32,267,304,658,c['inset'],radius=0)
        self.text(52,301,'EXPLORER',11,c['muted'],650,tracking=1)
        self.text(312,301,'↻   −',17,c['muted'],anchor='end')
        self.line(336,267,336,925,c['border'])
        rows=[(0,'▾  gui_e68b330b…','folder'),(1,'▾  001_validation','folder'),(2,'▾  reports','folder'),(3,'▾  verification_reports','folder'),(4,'▾  session','folder'),(5,'{ }  final_system_report.json','selected'),(5,'▸  agent1','folder'),(5,'▸  agent2','folder'),(5,'▸  agent3','folder'),(1,'▸  002_backend','folder'),(1,'{ }  approved_specs.yaml','file'),(1,'{ }  state.json','file'),(1,'≡   worker.log','file'),(0,'▸  External Artifacts','folder')]
        for i,(indent,text,kind) in enumerate(rows):
            y=343+i*34
            if kind=='selected':self.box(33,y-23,302,32,c['raised'],radius=0);self.box(33,y-23,3,32,c['gold'],radius=0)
            # Short enough to fit this conceptual tree at its fixed preview width.
            if kind=='selected':text='{ }  final_system_report…'
            self.text(49+indent*12,y,text,12,c['text'] if kind!='folder' else c['muted'],600 if kind=='selected' else 450)
        self.box(337,267,844,57,c['raised'],radius=0)
        self.text(358,302,'{ }',16,c['gold'],650,mono=True)
        self.text(396,302,'final_system_report.json',16,c['text'],650)
        self.button(1016,276,146,'Open Externally',h=36)
        self.text(358,352,'001_validation  ›  reports  ›  verification_reports  ›  session',12,c['muted'])
        self.line(337,370,1182,370,c['border'])
        code=['{','  "mode": "system",','  "top_module": "ddr4_controller_top",','  "status": "PASS_WITH_GAPS",','  "agents": {','    "agent1": "PASS",','    "agent2": "PASS",','    "agent3": "PASS_WITH_GAPS"','  },','  "requirements": {','    "passed": 15,','    "failed": 0,','    "not_tested": 0','  },','  "ready_for_backend": true','}']
        for i,line in enumerate(code):
            self.text(371,410+i*29,f'{i+1:02}',11,c['muted'],mono=True)
            self.text(410,410+i*29,line,14,'#e5c987' if 'PASS_WITH_GAPS' in line else '#a9d7e6' if '"' in line else c['text'],mono=True)
        self.text(358,905,'Read-only preview',11,c['muted']);self.text(1158,905,'Full evidence stays in the original run folder.',11,c['muted'],anchor='end')

    def render(self):
        self.header();self.saved_runs();getattr(self,self.screen)()
        self.line(32,955,1568,955,self.c['border'])
        self.dot(39,978,3,'#85dbea',True)
        self.text(53,982,'Worker finished' if self.screen=='results' else 'Worker connected',12,self.c['muted'])
        self.text(1568,982,'Design review mockup · Example content',11,self.c['muted'],anchor='end')
        self.parts.append('</svg>')
        return ''.join(self.parts)


def build():
    for key in STYLES:
        for screen in ('running','approval','results','files'):
            (ROOT/f'{key}-{screen}.svg').write_text(Draft(key,screen).render())
    gallery = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ramageddon · Gold Engineering Drafts</title>
<style>*{box-sizing:border-box}body{margin:0;background:#0f202f;color:#ecf2f5;font:15px system-ui,sans-serif}header,main{max-width:1648px;margin:auto;padding:24px}h1{font-size:24px;margin:0 0 10px}p{line-height:1.6;max-width:1000px;color:#b9cbd6}nav{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}button{font:inherit;background:#233f54;color:#edf2f4;padding:12px 18px;border:1px solid #506c80;border-radius:7px;cursor:pointer}button[aria-pressed=true]{background:#d6b464;color:#102535;border-color:#e4c98e;font-weight:700}button:focus-visible{outline:3px solid #85dbea;outline-offset:3px}#frame{width:100%;display:block;aspect-ratio:1.6;border:1px solid #506c80;border-radius:8px;background:#18334b}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px;margin-top:24px}article{padding:20px;border:1px solid #3e5b70;border-radius:8px;background:#172d40}h2{font-size:18px;margin-top:0}article p{margin-bottom:0}.note{font-size:13px}a{color:#e2bf78}.recommendation{border-left:3px solid #d6b464;padding-left:16px}</style>
<header><h1>Ramageddon · Gold Engineering</h1><p>Three visual directions for the Qt desktop GUI. These are static interface mockups with example data. The controls below switch the design and screen; controls drawn inside the mockups do not run anything.</p><nav aria-label="Design direction"><button data-style="a" aria-pressed="true">A · Blueprint + Brass</button><button data-style="b" aria-pressed="false">B · Precision Console</button><button data-style="c" aria-pressed="false">C · Engineering Studio</button></nav><nav aria-label="Screen"><button data-screen="running" aria-pressed="true">Active Run</button><button data-screen="approval" aria-pressed="false">Waiting for Approval</button><button data-screen="results" aria-pressed="false">Results</button><button data-screen="files" aria-pressed="false">Files</button></nav><p id="caption">Familiar blue. Intentional gold. Clear hierarchy.</p></header>
<main><img id="frame" src="a-running.svg" alt="Blueprint and Brass active run mockup"><p><a id="full" href="a-running.svg" target="_blank" rel="noopener">Open full-size vector draft</a></p><section><article><h2>A · Blueprint + Brass</h2><p>The closest evolution of today's interface. Navy panels, restrained brass contact details, cyan activity, and thin gold navigation accents. Recommended starting point.</p></article><article><h2>B · Precision Console</h2><p>Sharper corners, numbered stages, stronger horizontal rules, and a gold instrument-panel header. More technical and dense; risks feeling severe.</p></article><article><h2>C · Engineering Studio</h2><p>Brighter slate panels, softer corners, and warmer gold. Feels more approachable; less like a traditional EDA tool.</p></article></section><p class="recommendation"><strong>Recommendation:</strong> A's palette and PCB details, C's softer panel treatment, and B's numbered stages. Keep gold for structure and actions, cyan for running, green for success, and orange plus explicit text for required user input.</p><p class="note">Implementation direction: Qt layouts and vector icons, a restrained type scale, optional licensed bundled fonts, keyboard-visible focus, and explicit 100/150/200% scaling checks. These drafts show intended appearance, not validated Linux, WSLg, or macOS rendering. No working GUI behavior has changed.</p></main>
<script>const names={a:'Blueprint + Brass',b:'Precision Console',c:'Engineering Studio'};const captions={a:'Familiar blue. Intentional gold. Clear hierarchy.',b:'A precise, compact control-room instrument.',c:'Softer slate blue. Rounded panels. Warm brass.'};let style='a',screen='running';function update(){const path=style+'-'+screen+'.svg';document.getElementById('frame').src=path;document.getElementById('frame').alt=names[style]+' '+screen+' mockup';document.getElementById('full').href=path;document.getElementById('caption').textContent=captions[style];document.querySelectorAll('[data-style]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.style===style)));document.querySelectorAll('[data-screen]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.screen===screen)))}document.querySelectorAll('[data-style]').forEach(b=>b.onclick=()=>{style=b.dataset.style;update()});document.querySelectorAll('[data-screen]').forEach(b=>b.onclick=()=>{screen=b.dataset.screen;update()});</script></html>'''
    (ROOT/'index.html').write_text(gallery)


if __name__=='__main__':
    build()
    # Raster previews are rendered from the same vectors by Qt, without a browser.
    from PySide6.QtWidgets import QApplication
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QImage, QPainter, QColor, QFont
    from PySide6.QtCore import QRectF, Qt
    app=QApplication([])
    for key,screen in [('a','running'),('b','running'),('c','running'),('a','approval'),('a','results'),('a','files')]:
        renderer=QSvgRenderer(str(ROOT/f'{key}-{screen}.svg'))
        if not renderer.isValid():raise ValueError(f'Invalid SVG: {key}-{screen}')
        image=QImage(1600,1000,QImage.Format.Format_ARGB32);image.fill(QColor('#18334b'))
        painter=QPainter(image);renderer.render(painter);painter.end();image.save(str(ROOT/f'{key}-{screen}.png'))
    board=QImage(1800,580,QImage.Format.Format_ARGB32);board.fill(QColor('#0f202f'))
    painter=QPainter(board)
    for i,key in enumerate(STYLES):
        c=STYLES[key];x=24+i*594
        painter.setPen(QColor(c['gold']));font=QFont('Noto Sans');font.setPixelSize(22);font.setBold(True);painter.setFont(font)
        painter.drawText(x,42,f'{key.upper()}  {c["name"]}')
        painter.drawImage(QRectF(x,66,564,352.5),QImage(str(ROOT/f'{key}-running.png')))
        painter.setPen(QColor('#c0cfda'));font.setPixelSize(15);font.setBold(False);painter.setFont(font)
        lines={'a':['Current blue foundation, warmer gold.','Fine PCB contacts; clean navigation.','Best foundation for the next iteration.'],
               'b':['Sharper geometry and numbered stages.','Instrument-panel gold and tighter rhythm.','Strong engineering identity; more severe.'],
               'c':['Lighter slate surfaces and softer corners.','Warm gold with a calmer hierarchy.','Most approachable; borrow its softness.']}[key]
        for j,line in enumerate(lines):painter.drawText(x,454+j*28,line)
        for j,color in enumerate((c['bg'],c['panel'],c['gold'],'#85dbea')):
            painter.fillRect(x+j*39,548,30,8,QColor(color))
    painter.end();board.save(str(ROOT/'comparison.png'))
    print('Built twelve vector screen drafts, six full-size PNG previews, and comparison gallery.')
