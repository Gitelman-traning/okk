/* Один вердикт на встречу. Подставляется сборкой во все страницы вместо /*__VERDICT__*\/.
   Балл 0–100 из четырёх частей: цель (30), обязательные элементы «прозвучало» (30), этапы (25), речь в норме (15).
   Части, по которым нет данных, из знаменателя выпадают. Красная зона — провал элементов комитета/цены/закрытия:
   она перекрывает балл и красит встречу, даже если сумма высокая. Формула обсуждаемая — правится здесь, в одном месте. */
var VERDICT_STAGES=["контакт","потребности","презентация","возражения","фиксация"];
var VERDICT_RED={
  komitet4:"«комитет» прозвучал меньше 4 раз",
  komitet_meanings:"комитет не объяснён в разных смыслах",
  mechanics:"механика заявки и комитета не проговорена",
  doubts:"не спросил про сомнения",
  firmness:"не замерил твёрдость решения по шкале",
  tools_linked:"презентация без привязки к словам клиента",
  request:"запрос клиента не выявлен"
};
function verdictOf(d){
  var r=d.review||{}, chk=r.checklist||[];
  var pres=chk.filter(function(c){return c.present===true}).length;
  var tot=chk.filter(function(c){return c.present!=null}).length;
  var parts=[
    [d.achieved==="Да"?30:d.achieved==="Частично"?15:d.achieved==="Нет"?0:null,30],
    [tot?Math.round(30*pres/tot):null,30],
    [(function(){var v=d.s.filter(function(x){return typeof x==="number"});return v.length?Math.round(25*(v.reduce(function(a,b){return a+b},0)/v.length)/100):null})(),25],
    [typeof d.speech==="number"?(d.speech>=45&&d.speech<=60?15:d.speech>=35&&d.speech<=70?8:0):null,15]
  ].filter(function(p){return p[0]!=null});
  var max=parts.reduce(function(a,p){return a+p[1]},0);
  var score=max?Math.round(100*parts.reduce(function(a,p){return a+p[0]},0)/max):null;

  var red=[], byId={}; chk.forEach(function(c){if(c&&c.id)byId[c.id]=c});
  Object.keys(VERDICT_RED).forEach(function(id){var c=byId[id]; if(c&&c.present===false) red.push(VERDICT_RED[id])});
  if(d.next_date===false) red.push("следующий шаг без даты");
  if(d.banned) red.push("сказал «тренинг каждый месяц»");
  if(!d.komitet && !byId.komitet4) red.push("«комитет» не прозвучал");

  var level=(d.achieved==="Нет"||red.length>=3||(score!=null&&score<50))?"bad"
    :(red.length||d.achieved==="Частично"||(score!=null&&score<70))?"mid":"ok";
  var word={bad:"Провал",mid:"Риск",ok:"Прошла"}[level];

  var worst=null, wi=-1;
  d.s.forEach(function(v,i){if(typeof v==="number"&&(worst==null||v<worst)){worst=v;wi=i}});
  var main=red[0]||(wi>=0&&worst<75?"слабый этап — "+VERDICT_STAGES[wi]+" "+worst:"")||"";
  var advice=(r.stages||[]).map(function(s){return s&&s.fix}).filter(Boolean)[0]||"";
  var cs=r.client_scores||{};
  var scores=[["autonomy","автономность"],["initiative","инициативность"],["belief","вера"],["value","ценность"],["comfort","комфорт"]]
    .filter(function(x){return typeof cs[x[0]]==="number"}).map(function(x){return x[1]+" "+cs[x[0]]});
  return {score:score,level:level,word:word,red:red,main:main,advice:advice,
          elPres:pres,elTot:tot,scores:scores,why:r.goal_reason||"",summary:r.summary||""};
}
function verdictRank(v){return (v.level==="bad"?0:v.level==="mid"?1:2)*1000+(v.score==null?999:v.score)}
