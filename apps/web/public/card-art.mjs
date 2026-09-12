/** Forecast identity folio: original reflective geometry, never a data visualization. */
export const CARD_FONT='"Forecast Sora", "Apple SD Gothic Neo", "Hiragino Sans", "PingFang TC", "Noto Sans KR", "Noto Sans JP", "Noto Sans TC", sans-serif';
export const CARD_MATERIALS=Object.freeze({
  paper:Object.freeze({background:'#edf0f8',ink:'#17223d',muted:'#4d5c77',line:'#bfc9dc',accent:'#344cf1',quiet:'#dbe2f2',accentInk:'#ffffff',fold:['#c4c4ef','#d3eeee','#f1def1','#a9b8e5','#eaf8f1']}),
  ink:Object.freeze({background:'#172441',ink:'#f2f5fc',muted:'#bac8df',line:'#465675',accent:'#adc8ff',quiet:'#31415f',accentInk:'#172441',fold:['#424f83','#648994','#686791','#a5b2c6','#425486']}),
});
let fontReady;
/** Loading the unused canvas face explicitly prevents a cold first export using an accidental font. */
export function loadCardFonts(document=globalThis.document){
  if(!document?.fonts?.load)return Promise.resolve(false);
  if(!fontReady)fontReady=(async()=>{
    let timer;
    try{
      return await Promise.race([
        Promise.all([500,650,750].map(weight=>document.fonts.load(`${weight} 48px "Forecast Sora"`,'spritz.skr Forecast 0123456789'))).then(faces=>faces.every(group=>group.length>0)),
        new Promise(resolve=>{timer=setTimeout(()=>resolve(false),4000);}),
      ]);
    }catch{return false;}finally{clearTimeout(timer);}
  })();
  return fontReady;
}
export function cardFont(ctx,size,weight=500){ctx.font=`${weight} ${size}px ${CARD_FONT}`;}
export function cardMaterial(ctx,width,height,theme){
  ctx.fillStyle=theme.background;ctx.fillRect(0,0,width,height);
  const depth=height>width?250:230;
  const foil=ctx.createLinearGradient(width*.7,0,width,depth);
  theme.fold.forEach((color,index)=>foil.addColorStop(index/(theme.fold.length-1),color));
  ctx.fillStyle=foil;ctx.beginPath();ctx.moveTo(width*.7,0);ctx.lineTo(width,0);ctx.lineTo(width,depth);ctx.lineTo(width*.87,depth*.65);ctx.closePath();ctx.fill();
  ctx.save();ctx.beginPath();ctx.moveTo(width*.7,0);ctx.lineTo(width,0);ctx.lineTo(width,depth);ctx.lineTo(width*.87,depth*.65);ctx.closePath();ctx.clip();
  ctx.globalAlpha=.3;ctx.strokeStyle=theme.background;ctx.lineWidth=1;
  for(let i=0;i<15;i++){ctx.beginPath();ctx.moveTo(width*.63+i*23,0);ctx.lineTo(width+i*6,depth+30);ctx.stroke();}
  ctx.restore();
  ctx.strokeStyle=theme.line;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(24,height-24);ctx.lineTo(24,24);ctx.lineTo(width*.62,24);ctx.stroke();
  ctx.beginPath();ctx.moveTo(width-24,depth+28);ctx.lineTo(width-24,height-24);ctx.lineTo(width*.72,height-24);ctx.stroke();
}
export function cardRule(ctx,x,y,width,color){ctx.fillStyle=color;ctx.fillRect(x,y,width,1);}
export function cardBrand(ctx,x,y,theme,size=28){cardFont(ctx,size,750);ctx.fillStyle=theme.ink;ctx.fillText('forecast',x,y);const width=ctx.measureText('forecast').width;ctx.fillStyle=theme.accent;ctx.fillText('.',x+width,y);}
