import json, re, io

VTT = r"C:\Users\Nikita\Downloads\GMT20260828-075246_Recording.transcript.vtt"
DG  = r"C:\Users\Nikita\Documents\Gitelman\Local\zoom-transcript.json"

def ts(s):
    h,m,rest = s.split(':')
    sec,ms = rest.split('.')
    return int(h)*3600+int(m)*60+int(sec)+int(ms)/1000

cues=[]
block=[]
for line in io.open(VTT, encoding='utf-8-sig'):
    line=line.rstrip('\n').rstrip('\r')
    if line=='':
        if len(block)>=2 and '-->' in block[1]:
            a,b = block[1].split('-->')
            text=' '.join(block[2:])
            spk, _, txt = text.partition(':')
            cues.append({'start':ts(a.strip()),'end':ts(b.strip()),'speaker':spk.strip(),'zoom':txt.strip()})
        block=[]
    else:
        block.append(line)

dg=json.load(io.open(DG,encoding='utf-8'))
words=dg['results']['channels'][0]['alternatives'][0]['words']

wi=0
for c in cues:
    buf=[]
    while wi < len(words) and words[wi]['start'] < c['end']:
        if words[wi]['end'] > c['start']-0.3:
            buf.append(words[wi].get('punctuated_word') or words[wi]['word'])
        wi+=1
    c['dg']=' '.join(buf)

# схлопываем подряд идущие реплики одного спикера
merged=[]
for c in cues:
    if merged and merged[-1]['speaker']==c['speaker'] and c['start']-merged[-1]['end']<8:
        merged[-1]['dg'] = (merged[-1]['dg']+' '+c['dg']).strip()
        merged[-1]['zoom'] = (merged[-1]['zoom']+' '+c['zoom']).strip()
        merged[-1]['end']=c['end']
    else:
        merged.append(dict(c))

def mmss(s): return f"{int(s//60):02d}:{int(s%60):02d}"

with io.open('rechka_dg.txt','w',encoding='utf-8') as f:
    for c in merged:
        if not c['dg'].strip(): continue
        f.write(f"[{mmss(c['start'])}] {c['speaker']}: {c['dg']}\n")

with io.open('rechka_compare.txt','w',encoding='utf-8') as f:
    for c in merged[:25]:
        f.write(f"[{mmss(c['start'])}] {c['speaker']}\n  ZOOM: {c['zoom']}\n  DG  : {c['dg']}\n\n")

print('cues', len(cues), 'merged', len(merged), 'words', len(words))
spk={}
for c in merged: spk[c['speaker']]=spk.get(c['speaker'],0)+ (c['end']-c['start'])
print({k:round(v/60,1) for k,v in sorted(spk.items(), key=lambda x:-x[1])})
