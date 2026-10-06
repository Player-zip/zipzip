# Writes the motion-broll clip fragments (clips/*.html) with clip-local word times
# taken from work/times.json. Transparent 720x1280 panels that live in the sky above
# the speaker's head (y < ~400), dark shapes + green accent so they read on a bright sky.
import json

T = json.load(open("work/times.json"))
COMMON = """
<style>
.k{white-space:nowrap;font-family:'Geist Mono',monospace;font-size:17px;letter-spacing:.14em;text-transform:uppercase;color:#22D46A}
.h{font-size:34px;font-weight:600;letter-spacing:-.02em;color:#fff;white-space:nowrap}
.mt{color:#A3ABA6}
.tk{position:absolute;height:4px;border-radius:2px;background:rgba(255,255,255,.16)}
.tf{position:absolute;height:4px;border-radius:2px;background:#22D46A}
.dot{position:absolute;width:18px;height:18px;margin:-7px 0 0 -9px;border-radius:50%;background:#22D46A;box-shadow:0 0 0 5px rgba(34,212,106,.22)}
.yr{position:absolute;width:160px;margin-left:-80px;text-align:center;font-size:30px;font-weight:600;color:#fff}
.lb{position:absolute;width:180px;margin-left:-90px;text-align:center;font-size:18px;color:#A3ABA6}
.rw{position:absolute;left:-280px;width:560px}
.rw b{display:block;font-size:28px;font-weight:600;color:#fff}
.rw span{display:block;font-size:19px;color:#A3ABA6;margin-top:4px;white-space:normal}
.chip{display:inline-flex;align-items:center;gap:8px;padding:9px 18px;border-radius:999px;background:rgba(34,212,106,.16);color:#fff;font-size:21px;font-weight:500}
.chip i{width:9px;height:9px;border-radius:50%;background:#22D46A}
</style>"""
HELP = """
const $=id=>document.getElementById(id);
function pop(el,t,tin,dy=12){const v=M.vis(t,tin,null,{din:0,lin:0.24});el.style.opacity=v.o;
  el.style.filter=v.blur>0.05?`blur(${v.blur}px)`:'none';el.style.transform=`translateY(${(1-v.a)*dy}px)`;}
"""


def local(keys, t0):
    return json.dumps({k: round(T[k] - t0, 3) for k in keys})


# ---------------- 01 formação: pill -> Educação Física timeline -> Nutrição timeline ----------------
t0 = T["hist"] - 0.15
c1 = f"""<title>01 Formação</title>{COMMON}
<div data-slot="shape">
  <div class="L" id="Lp"><div class="row" style="transform:translate(-50%,-50%);gap:12px;color:#fff;font-size:26px;font-weight:500"><i style="width:12px;height:12px;border-radius:50%;background:#22D46A"></i>Minha história profissional</div></div>
  <div class="L" id="Lef">
    <div class="a k" style="left:-280px;top:30px">formação · UEL</div>
    <div class="a h" style="left:-280px;top:58px">Educação Física</div>
    <div class="tk" style="left:-250px;top:160px;width:500px"></div><div class="tf" id="ef_f" style="left:-250px;top:160px;width:0"></div>
    <div id="e0"><div class="dot" style="left:-250px;top:160px"></div><div class="yr" style="left:-250px;top:186px">2008</div><div class="lb" style="left:-250px;top:226px">o interesse</div></div>
    <div id="e1"><div class="dot" style="left:0;top:160px"></div><div class="yr" style="left:0;top:186px">2009</div><div class="lb" style="left:0;top:226px">início do curso</div></div>
    <div id="e2"><div class="dot" style="left:250px;top:160px"></div><div class="yr" style="left:250px;top:186px">2012</div><div class="lb" style="left:250px;top:226px">formado</div></div>
    <div class="a" id="e10" style="left:-280px;top:272px"><span class="chip"><i></i>10 anos na área</span></div>
  </div>
  <div class="L" id="Lnu">
    <div class="a k" style="left:-280px;top:30px">a vontade de sempre</div>
    <div class="a h" style="left:-280px;top:58px">Nutrição</div>
    <div class="tk" style="left:-200px;top:160px;width:400px"></div><div class="tf" id="nu_f" style="left:-200px;top:160px;width:0"></div>
    <div id="n0"><div class="dot" style="left:-200px;top:160px"></div><div class="yr" style="left:-200px;top:186px">2018</div><div class="lb" style="left:-200px;top:226px">início do curso</div></div>
    <div id="n1"><div class="dot" style="left:200px;top:160px"></div><div class="yr" style="left:200px;top:186px">2021</div><div class="lb" style="left:200px;top:226px">formado · Nopar</div></div>
  </div>
</div><!--/shape-->
<script>{HELP}
const T={local(["hist","f_in","f2008","f2009","f2012","fuel","f10","n_in","n2018","n2021","nopar","f_out"], t0)};
const efW=M.track(0,[[T.f2009,250,M.SLOW],[T.f2012,500,M.SLOW]]), nuW=M.track(0,[[T.n2021,400,M.SLOW]]);
M.scene({{
  W:720,H:1280,bg:null,T:T.f_out+0.6,center:[360,230],intro:0.15,
  SH:{{pill:{{w:470,h:72,r:36,bg:'#0B0B0B',cam:1}}, ef:{{w:640,h:330,r:30,bg:'#0B0B0B',cam:1}},
       efx:{{w:640,h:350,r:30,bg:'#0B0B0B',cam:1}}, nu:{{w:640,h:290,r:30,bg:'#0B0B0B',cam:1}}, end:{{w:120,h:40,r:20,bg:'#0B0B0B',cam:1}}}},
  start:'pill', SEQ:[[T.f_in,'ef'],[T.f10-0.05,'efx'],[T.n_in,'nu'],[T.f_out,'end']],
  layers:[
    {{el:'Lp',tin:0.15,tout:T.f_in}},
    {{el:'Lef',tin:T.f_in,tout:T.n_in,anchor:'t',update:(t)=>{{
      $('ef_f').style.width=Math.max(0,efW(t))+'px';
      pop($('e0'),t,T.f2008-0.1); pop($('e1'),t,T.f2009); pop($('e2'),t,T.f2012); pop($('e10'),t,T.f10);
    }}}},
    {{el:'Lnu',tin:T.n_in,tout:T.f_out,anchor:'t',update:(t)=>{{
      $('nu_f').style.width=Math.max(0,nuW(t))+'px'; pop($('n0'),t,T.n2018); pop($('n1'),t,T.n2021);
    }}}},
  ],
}});
</script>
"""

# ---------------- 02 credenciais -> atuação ----------------
t0 = T["c_in"] - 0.15
c2 = f"""<title>02 Credenciais</title>{COMMON}
<div data-slot="shape">
  <div class="L" id="Lpos">
    <div class="a k" style="left:-280px;top:30px">sempre buscando conhecimento</div>
    <div class="a h" style="left:-280px;top:58px">2 pós-graduações</div>
    <div class="rw" id="p0" style="top:122px"><b>USP</b><span>Nutrição esportiva e obesidade</span></div>
    <div class="rw" id="p1" style="top:206px"><b>Unibasul</b><span>Nutrição e fisiologia aplicadas ao exercício</span></div>
  </div>
  <div class="L" id="Lcert">
    <div class="a k" style="left:-280px;top:30px">nutrição esportiva</div>
    <div class="a h" style="left:-280px;top:58px">2 certificações internacionais</div>
    <div class="row" id="q0" style="left:-280px;top:130px;gap:14px;font-size:28px;font-weight:600;color:#fff"><span class="ic"></span>ACSM</div>
    <div class="row" id="q1" style="left:-280px;top:186px;gap:14px;font-size:28px;font-weight:600;color:#fff"><span class="ic"></span>Clube Barcelona</div>
  </div>
  <div class="L" id="Labne">
    <div class="a k" style="left:-280px;top:30px">membro</div>
    <div class="a" style="left:-280px;top:58px;width:560px;font-size:30px;font-weight:600;line-height:1.2;color:#fff">Associação Brasileira de Nutrição Esportiva</div>
  </div>
  <div class="L" id="Latu">
    <div class="a k" style="left:-280px;top:30px">desde então</div>
    <div class="a" style="left:-280px;top:58px;width:560px;font-size:30px;font-weight:600;line-height:1.2;color:#fff">Nutricionista especialista em <span style="color:#22D46A">emagrecimento</span></div>
    <div class="row" style="left:-280px;top:170px;gap:10px"><span class="chip" id="l0"><i></i>Londrina</span><span class="chip" id="l1"><i></i>Maringá</span><span class="chip" id="l2"><i></i>Online</span></div>
  </div>
</div><!--/shape-->
<script>{HELP}
const T={local(["c_in","usp","uni","cert","acsm","bar","abne","a_in","emag","lon","mar","onl","a_out"], t0)};
document.querySelectorAll('#Lcert .ic').forEach(e=>e.innerHTML=M.icon('check',30,'#22D46A',2.6));
M.scene({{
  W:720,H:1280,bg:null,T:T.a_out+0.6,center:[360,230],intro:0.15,
  SH:{{pos:{{w:640,h:310,r:30,bg:'#0B0B0B',cam:1}}, cert:{{w:640,h:250,r:30,bg:'#0B0B0B',cam:1}},
       abne:{{w:640,h:170,r:30,bg:'#0B0B0B',cam:1}}, atu:{{w:640,h:240,r:30,bg:'#0B0B0B',cam:1}}, end:{{w:120,h:40,r:20,bg:'#0B0B0B',cam:1}}}},
  start:'pos', SEQ:[[T.cert,'cert'],[T.abne-0.1,'abne'],[T.a_in,'atu'],[T.a_out,'end']],
  layers:[
    {{el:'Lpos',tin:0.15,tout:T.cert,anchor:'t',update:(t)=>{{pop($('p0'),t,T.usp);pop($('p1'),t,T.uni);}}}},
    {{el:'Lcert',tin:T.cert,tout:T.abne-0.1,anchor:'t',update:(t)=>{{pop($('q0'),t,T.acsm);pop($('q1'),t,T.bar);}}}},
    {{el:'Labne',tin:T.abne-0.1,tout:T.a_in,anchor:'t'}},
    {{el:'Latu',tin:T.a_in,tout:T.a_out,anchor:'t',update:(t)=>{{pop($('l0'),t,T.lon,8);pop($('l1'),t,T.mar,8);pop($('l2'),t,T.onl,8);}}}},
  ],
}});
</script>
"""

# ---------------- 03 CTA: pill -> "Seguir" button clicked on "me siga" ----------------
t0 = T["cta_in"] - 0.15
c3 = f"""<title>03 Siga</title>{COMMON}
<div data-slot="shape">
  <div class="L" id="La"><div class="row" style="transform:translate(-50%,-50%);gap:12px;color:#fff;font-size:26px;font-weight:500"><i style="width:12px;height:12px;border-radius:50%;background:#22D46A"></i>Acompanhe meu conteúdo</div></div>
  <div class="L" id="Lb"><div class="row" style="transform:translate(-50%,-50%);gap:10px;color:#0B0B0B;font-size:32px;font-weight:600"><span id="plus"></span>Seguir</div></div>
  <div class="L" id="Lc"><div class="row" style="transform:translate(-50%,-50%);gap:10px;color:#0B0B0B;font-size:32px;font-weight:600"><span id="chk"></span>Seguindo</div></div>
</div><!--/shape-->
<script>{HELP}
const T={local(["cta_in","siga","cta_out"], t0)};
$('plus').innerHTML=M.icon('plus',30,'#0B0B0B',2.8); $('chk').innerHTML=M.icon('check',30,'#0B0B0B',2.8);
const B=T.siga-1.1;
M.scene({{
  W:720,H:1280,bg:null,T:T.cta_out-0.0,center:[360,250],intro:0.15,
  SH:{{a:{{w:450,h:72,r:36,bg:'#0B0B0B',cam:1}}, b:{{w:260,h:92,r:46,bg:'#22D46A',cam:1}}, c:{{w:300,h:92,r:46,bg:'#F4F2EE',cam:1}}}},
  start:'a', SEQ:[[B,'b'],[T.siga+0.05,'c']],
  layers:[{{el:'La',tin:0.15,tout:B}},{{el:'Lb',tin:B,tout:T.siga+0.05}},{{el:'Lc',tin:T.siga+0.05,tout:null}}],
  cursor:{{size:40,clicks:[T.siga],keys:[[0,260,200],[B+0.2,260,200],[T.siga-0.15,30,14],[T.siga+0.6,40,20],[T.cta_out,120,150]]}},
}});
</script>
"""
for name, c in (("01-formacao", c1), ("02-credenciais", c2), ("03-siga", c3)):
    open(f"clips/{name}.html", "w").write(c)
print("clip starts:", {k: round(T[k] - 0.15, 2) for k in ("hist", "c_in", "cta_in")})
