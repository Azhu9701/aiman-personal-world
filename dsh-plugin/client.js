// DSH native browser entry: use its real sidebar slot, never scrape its DOM.
if(typeof window!=='undefined'&&window.__ModuleLoader__){
  window.__ModuleLoader__.load({
    id:'@aiman-world/dsh-personal-world',
    factory(require){
      const React=require('react');
      function Entry(props){
        return React.createElement('button',{
          type:'button',title:'个人世界：聊天、画像、日程和行动工作台',
          'data-personal-world-entry':'',
          style:{display:'flex',alignItems:'center',gap:8,width:'100%',padding:'7px 10px',border:0,borderRadius:8,background:'transparent',color:'var(--dsw-alias-label-secondary)',font:'inherit',cursor:'pointer',textAlign:'left'},
          onClick:()=>window.open('/personal-world/','_blank','noopener,noreferrer')
        },React.createElement('span',{'aria-hidden':true},'◎'),props.wide===false?null:React.createElement('span',null,'个人世界'));
      }
      function apply(ctx){
        const slots=ctx.get?.('slots');
        if(!slots)return;
        ctx.effect(()=>{
          let unregister=()=>{};
          const removeListener=slots.inject('sidebar.footer.action',()=>{
            unregister=slots.register({name:'sidebar.footer.action',id:'aiman-personal-world',order:18,label:'个人世界'},props=>React.createElement(Entry,props));
          });
          return()=>{unregister();if(typeof removeListener==='function')removeListener();};
        },'personal-world sidebar entry');
      }
      return {inject:['slots'],apply};
    }
  });
}
