/* Small DOM-only Markdown renderer. Never evaluates model HTML or URLs. */
window.AmberText={render(target,text){
  function inline(parent,value){
    const pattern=/(\*\*([^*]+)\*\*|`([^`]+)`|\*([^*]+)\*|\[([^\]]+)\]\(([^)]+)\))/g;
    let last=0,match;
    while((match=pattern.exec(value))){parent.append(document.createTextNode(value.slice(last,match.index)));let node;
      if(match[2]){node=document.createElement('strong');node.textContent=match[2];}
      else if(match[3]){node=document.createElement('code');node.textContent=match[3];}
      else if(match[4]){node=document.createElement('em');node.textContent=match[4];}
      else{node=document.createElement('span');node.textContent=match[5];node.title=match[6];node.className='text-link';}
      parent.append(node);last=pattern.lastIndex;
    }parent.append(document.createTextNode(value.slice(last)));
  }
  const fragment=document.createDocumentFragment();let code=null,list=null,table=null;
  for(const line of String(text||'').split('\n')){
    if(/^\s*```/.test(line)){if(code){code=null;}else{code=document.createElement('pre');fragment.append(code);}list=null;continue;}
    if(code){code.textContent+=line+'\n';continue;}
    let match=line.match(/^\s*(#{1,3})\s+(.+)$/);
    if(match){const h=document.createElement('h'+match[1].length);inline(h,match[2]);fragment.append(h);list=null;table=null;continue;}
    match=line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)$/);
    if(match){if(!list){list=document.createElement('ul');fragment.append(list);}const li=document.createElement('li');inline(li,match[1]);list.append(li);table=null;continue;}
    list=null;
    if(/^\s*\|.*\|\s*$/.test(line)){
      if(/^\s*\|[\s:|\-]+\|\s*$/.test(line))continue;
      if(!table){table=document.createElement('table');fragment.append(table);}const row=document.createElement('tr');for(const cell of line.trim().slice(1,-1).split('|')){const td=document.createElement('td');inline(td,cell.trim());row.append(td);}table.append(row);continue;
    }table=null;
    const p=document.createElement(/^\s*>/.test(line)?'blockquote':'p');inline(p,line.replace(/^\s*>\s?/,''));if(!line)p.append(document.createElement('br'));fragment.append(p);
  }target.replaceChildren(fragment);
}};
