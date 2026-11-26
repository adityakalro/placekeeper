const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../placekeeper/web/templates/sync.html'), 'utf8').split('<script nonce="NONCE">')[1].split('</script>')[0];
const status = {connected:true,can_discover:true,running:false,running_job:null,phase:'Ready',services:{},
  books:[{key:'book',title:'Example',mode:'automatic',reading_status:'reading'}],
  last:{status:'complete',finished_at:'2026-01-01T00:00:00Z',books:[{title:'Example',status:'paused',message:'Paused.'}]},
  last_discovery:{discovery:{matches:[{title:'Example',status:'ready'}]}}};
const flush = () => new Promise(setImmediate);
function page(){
  const elements=new Map(),requests=[],timers=new Map(),events={};let next=0;
  class Element {
    constructor(){this.children=[];this.replacements=0;this.listeners={};this.classList={toggle(){}};}
    append(...children){this.children.push(...children);}
    replaceChildren(){this.children=[];this.replacements++;}
    insertRow(){const row=new Element();this.append(row);return row;}
    insertCell(){return this.insertRow();}
    setAttribute(){}
    addEventListener(name,callback){this.listeners[name]=callback;}
    contains(target){return target===this||this.children.some(child=>child.contains(target));}
  }
  const document={hidden:false,activeElement:null,createElement:()=>new Element(),
    getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
    addEventListener(name,callback){events[name]=callback;}};
  const context=vm.createContext({document,Map,Date,Promise,JSON,
    fetch:()=>new Promise(resolve=>requests.push(resolve)),
    setTimeout(callback,delay){const id=++next;timers.set(id,{callback,delay});return id;},
    clearTimeout(id){timers.delete(id);}});
  vm.runInContext(source,context);
  return {context,document,elements,requests,timers,events,
    async respond(value){assert.equal(requests.length,1);requests.shift()({ok:true,json:async()=>value});await flush();}};
}
test('unchanged status preserves tables and focused book controls',async()=>{
  const p=page();await p.respond(status);
  const ids=['bookControls','results','discoveryMatches'];
  const counts=ids.map(id=>p.elements.get(id).replacements);
  p.context.render(JSON.parse(JSON.stringify(status)));
  assert.deepEqual(ids.map(id=>p.elements.get(id).replacements),counts);
  const books=p.elements.get('bookControls');p.document.activeElement=books.children[0];
  const updated={...status,books:[{...status.books[0],mode:'paused'}]};
  p.context.render(updated);assert.equal(books.replacements,counts[0]);
  p.document.activeElement=null;p.context.render(updated);assert.equal(books.replacements,counts[0]+1);
  assert.equal(p.elements.get('results').replacements,counts[1]);
});
test('polling slows while idle, stays fast during jobs, and pauses in hidden tabs',async()=>{
  const p=page();await p.respond(status);
  assert.equal([...p.timers.values()][0].delay,30000);
  p.context.refresh();await p.respond({...status,running:true});
  assert.equal([...p.timers.values()][0].delay,2500);
  p.document.hidden=true;p.events.visibilitychange();assert.equal(p.timers.size,0);
  await p.context.refresh();assert.equal(p.requests.length,0);
  p.document.hidden=false;p.events.visibilitychange();assert.equal(p.requests.length,1);
  await p.respond(status);assert.equal([...p.timers.values()][0].delay,30000);
});
test('overlapping refresh calls share the request and queue one fresh check',async()=>{
  const p=page();p.context.refresh();p.context.refresh();assert.equal(p.requests.length,1);
  await p.respond(status);
  const queued=[...p.timers.values()][0];assert.equal(queued.delay,0);
  queued.callback();assert.equal(p.requests.length,1);
  await p.respond(status);assert.equal([...p.timers.values()][0].delay,30000);
});

test('failed job request restores buttons even when status is unchanged',async()=>{
  const p=page();await p.respond(status);
  const click=p.elements.get('sync').listeners.click();
  assert.equal(p.elements.get('sync').disabled,true);
  p.requests.shift()({ok:false,json:async()=>({error:'Could not start'})});
  await flush();await p.respond(status);await click;
  assert.equal(p.elements.get('sync').disabled,false);
  assert.equal(p.elements.get('discover').disabled,false);
});
