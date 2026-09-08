import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

/** Raw official renderer messages; tile IDs and game state remain opaque here. */
export class TileJournal {
  run = 0;
  batches: string[] = [];
  bytes = 0;
  error: string | null = null;
  reset(run: number) {
    this.run = run;
    this.batches = [];
    this.bytes = 0;
    this.error = null;
  }
  append(messages: Record<string, unknown>[]) {
    if (this.error) return;
    const batch = JSON.stringify(messages);
    if (this.bytes + batch.length > 64 * 1024 * 1024) {
      this.error = "Tile history limit reached. Terminal remains available.";
      return;
    }
    this.batches.push(batch);
    this.bytes += batch.length;
  }
}

/** Inventory only trusted renderer assets, never resolve a browser-supplied path. */
export function tileAssets(crawlPath: string) {
  const root = resolve(process.env.CRAWL_WEB_ASSETS ?? resolve(dirname(crawlPath), "webserver"));
  const files = new Map<string, string>();
  function inventory(directory: string, prefix: string) {
    if (!existsSync(directory)) return;
    for (const item of readdirSync(directory, { withFileTypes: true })) {
      if (item.isSymbolicLink()) continue;
      const file = resolve(directory, item.name);
      if (item.isDirectory()) inventory(file, prefix + item.name + "/");
      else if (/\.(js|css|png|gif|woff2?|ttf|svg)$/.test(item.name)) files.set(prefix + item.name, file);
    }
  }
  inventory(resolve(root, "static"), "/tiles/static/");
  inventory(resolve(root, "game_data/static"), "/tiles/game/");
  const template = resolve(root, "game_data/templates/game.html");
  const ready = existsSync(template) && files.has("/tiles/game/main.png") && files.has("/tiles/game/tileinfo-main.js");
  const markup = ready ? readFileSync(template, "utf8")
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "")
    .replaceAll("/gamedata/{{ version }}", "/tiles/game") : "";
  return { files, markup, ready };
}

export function tilePage(markup: string, ready: boolean) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Crawl tiles</title><link rel="stylesheet" href="/tiles/static/style.css"><link rel="stylesheet" href="/tiles/viewer.css"></head><body><div id="viewer-status" role="status">${ready ? "Waiting for game…" : "Tiles unavailable. Build the matching Crawl WebTiles assets first."}</div><div id="game">${markup}</div>${ready ? '<script src="/tiles/static/scripts/contrib/require.js"></script><script src="/tiles/viewer.js"></script>' : ""}</body></html>`;
}

export const tileStyle = `
html,body{margin:0;width:100%;height:100%;background:#000;color:#dedbd4;overflow:hidden}
body{font:13px/1.15 monospace}
#viewer-status{position:fixed;inset:0;display:grid;place-items:center;padding:24px;text-align:center;background:#000;z-index:9999;font:13px/1.6 sans-serif}
#viewer-status[hidden]{display:none}
#game{height:100%;background:#000}
#normal,#left_column,#right_column,#message_pane,#messages_container,#stats,#minimap_block,#monster_list{background:#000!important;border:0;box-shadow:none}
#stats,#message_pane,#monster_list,#crt{font-size:13px!important;line-height:15px!important}
#right_column{max-height:100%;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin}
#minimap_block{height:130px!important}
#minimap,#minimap_overlay{width:100%!important;height:130px!important;object-fit:contain}
#viewer-inventory{display:grid;grid-template-columns:repeat(auto-fill,32px);gap:2px;padding:8px 0;max-width:100%}
#viewer-inventory canvas{display:block;width:32px;height:32px;box-sizing:border-box;border:1px solid #242424;image-rendering:pixelated}
#mobile_input,#action-panel,#action-panel-placeholder{display:none!important}
`;

export const tileScript = `"use strict";
require.config({baseUrl:"/tiles/static/scripts",paths:{jquery:"/tiles/static/scripts/contrib/jquery",game:"/tiles/game"},waitSeconds:30});
define("client",["jquery"],function($){
  function set_layer(layer){["crt","normal"].forEach(function(name){$("#"+name).toggle(name===layer)});window.current_layer=layer}
  // Legacy globals expected by the unmodified generated tile modules.
  window.current_layer="crt";window.debug_mode=false;window.assert=function(){};window.abs=Math.abs;window.log=console.log.bind(console);window.set_layer=set_layer;
  return{is_watching:function(){return true},in_game:function(){return true},delay:function(){},set_layer:set_layer}
});
require.onError=function(){document.getElementById("viewer-status").textContent="Tiles could not load. Check the matching WebTiles assets."};
require(["jquery","comm","game/game","game/player","game/cell_renderer","game/tileinfo-main","game/util"],function($,comm,game,player,cr,main,util){
  (async function(){
  // There is deliberately no socket or parent-message input bridge.
  comm.send_message=function(){};
  comm.register_handlers({layer:function(message){window.set_layer(message.layer)},multi:function(message){message.msgs.forEach(function(item){if(!comm.handle_message_immediately(item))comm.handle_message(item)})}});
  window.current_layer="crt";
  const status=document.getElementById("viewer-status");
  try {
    await Promise.all(Array.from(document.querySelectorAll("img")).map(function(img){return img.decode()}));
    $(document).trigger("game_preinit");
    $(document).trigger("game_init");
    // Only public inventory render data is used; the grid cannot send input.
    const inventory=document.createElement("div");
    inventory.id="viewer-inventory";
    inventory.setAttribute("role","group");
    inventory.setAttribute("aria-label","Inventory");
    document.getElementById("right_column").appendChild(inventory);
    let inventoryVersion="";
    function renderInventory(){
      const version=JSON.stringify(player.inv);
      if(version===inventoryVersion)return;
      inventoryVersion=version;
      inventory.replaceChildren();
      if(!Object.keys(player.inv).length)return;
      for(let slot=0;slot<52;slot++){
        const item=player.inv[slot];
        const canvas=document.createElement("canvas");
        const label=item&&item.quantity?item.name:"Empty slot";
        canvas.title=label;canvas.setAttribute("role","img");canvas.setAttribute("aria-label",label);
        inventory.appendChild(canvas);
        util.init_canvas(canvas,32,32);
        const renderer=new cr.DungeonCellRenderer();
        renderer.init(canvas);renderer.clear();
        if(!item||!item.quantity)continue;
        const tiles=Array.isArray(item.tile)?item.tile:[item.tile];
        tiles.forEach(function(tile){if(Number.isFinite(tile))renderer.draw_tile(tile,0,0,main)});
        if(item.quantity>1)renderer.draw_quantity(String(item.quantity),0,0,"12px monospace");
      }
    }
    const source=new EventSource("/tiles/events");
    let run=null;
    source.onmessage=function(event){
      const batch=JSON.parse(event.data);
      if(run!==null && batch.run_id!==run){source.close();location.reload();return}
      run=batch.run_id;
      if(batch.error){status.hidden=false;status.textContent=batch.error;return}
      if(batch.messages.length) status.hidden=true;
      for(const message of batch.messages){
        if(message.msg==="flush_messages"||message.msg==="exit_reason")continue;
        try {if(!comm.handle_message_immediately(message))comm.handle_message(message)}
        catch(error){status.hidden=false;status.textContent="Tiles could not render this game update.";console.error(error);source.close();return}
      }
      renderInventory();
    };
  } catch(error){status.textContent="Tiles could not load. Check the matching WebTiles assets.";console.error(error)}
  })();
});`;

export function tileEvents(journal: TileJournal, request: Request, release: () => void) {
  let timer: ReturnType<typeof setInterval>;
  let disposed = false;
  const dispose = () => { if (!disposed) { disposed = true; clearInterval(timer); release(); } };
  const last = request.headers.get("Last-Event-ID")?.match(/^(\d+):(\d+)$/);
  let run = journal.run;
  let cursor = last && Number(last[1]) === run ? Math.min(Number(last[2]), journal.batches.length) : 0;
  let announced = false;
  return new ReadableStream<Uint8Array>({
    start(controller) {
      let sent = 0;
      const tick = () => {
        if (disposed || (controller.desiredSize ?? 0) <= 0) return;
        if (run !== journal.run) { run = journal.run; cursor = 0; announced = false; }
        let body: string | null = null;
        if (!announced || journal.error) {
          body = JSON.stringify({ run_id: run, messages: [], error: journal.error });
          announced = true;
        } else if (cursor < journal.batches.length) body = '{"run_id":' + run + ',"messages":' + journal.batches[cursor++] + '}';
        if (body !== null) {
          controller.enqueue(new TextEncoder().encode('id: ' + run + ':' + cursor + '\ndata: ' + body + '\n\n'));
          sent = Date.now();
        } else if (Date.now() - sent > 15000) {
          controller.enqueue(new TextEncoder().encode(': keepalive\n\n'));
          sent = Date.now();
        }
      };
      timer = setInterval(tick, 20);
      tick();
      request.signal.addEventListener("abort", () => { dispose(); try { controller.close(); } catch {} }, { once: true });
    },
    cancel: dispose,
  });
}
