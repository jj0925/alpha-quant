import { useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { api } from "../api/client";
import {
  ComposedChart, AreaChart, Area, Line, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer, ReferenceLine, Cell,
} from "recharts";
import MetricCard from "../components/ui/MetricCard";

const UP_COLOR = "#f87171";
const DOWN_COLOR = "#34d399";
const NEUTRAL_COLOR = "#94a3b8";

const T = ({active,payload,label})=>active&&payload?.length?(
  <div style={{background:"#1e293b",border:"1px solid rgba(255,255,255,0.08)",borderRadius:8,padding:"10px 14px",fontSize:11}}>
    <p style={{color:"#64748b",marginBottom:6}}>{label}</p>
    {payload.map(p=><p key={p.name} style={{color:p.color,fontFamily:"'JetBrains Mono',monospace",fontWeight:600}}>
      {p.name}: {typeof p.value==="number"?p.value.toFixed(4):p.value}
    </p>)}
  </div>
):null;

const pct = (v,d=2)=> v==null?"—":`${(v*100).toFixed(d)}%`;
const n   = (v,d=3)=> v==null?"—":v.toFixed(d);
const fallbackSourceFilename = (id)=>`run-${id}-source-data.csv`;

function getDownloadFilename(disposition, fallback) {
  if (!disposition) return fallback;
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }
  const plainMatch = disposition.match(/filename="?([^"]+)"?/i);
  return plainMatch?.[1] || fallback;
}

export default function BacktestPage() {
  const { id } = useParams();
  const [isDownloadingSourceData, setIsDownloadingSourceData] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey:["backtest",id],
    queryFn:()=>api.get(`/runs/${id}/backtest/`).then(r=>r.data),
    refetchInterval: d=>d?.metrics?false:3000,
  });

  const handleDownloadSourceData = async () => {
    if (!id || isDownloadingSourceData) return;
    setIsDownloadingSourceData(true);
    try {
      const response = await api.get(`/runs/${id}/source-data/`, { responseType:"blob" });
      const filename = getDownloadFilename(
        response.headers["content-disposition"],
        fallbackSourceFilename(id),
      );
      const blob = response.data instanceof Blob
        ? response.data
        : new Blob([response.data], { type:"text/csv;charset=utf-8" });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => window.URL.revokeObjectURL(url), 0);
      toast.success("原始資料已開始下載");
    } catch {
      toast.error("下載原始資料失敗");
    } finally {
      setIsDownloadingSourceData(false);
    }
  };

  if(isLoading) return (
    <div style={{display:"flex",alignItems:"center",justifyContent:"center",height:300}}>
      <div style={{width:36,height:36,borderRadius:"50%",border:"3px solid rgba(99,102,241,0.2)",borderTop:"3px solid #6366f1",animation:"spin 0.8s linear infinite"}}/>
    </div>
  );

  const m  = data?.metrics??{};
  const eq = data?.equity_curve??[];
  const bh = data?.bh_curve??[];
  const dd = data?.drawdown_curve??[];
  const pos= data?.position_log??[];

  const bhMap = new Map(bh.map(point => [point.date, point.value]));
  const combined = eq.map((e)=>({
    date:e.date,
    strategy:+Number(e.value ?? 1).toFixed(4),
    bh:+Number(bhMap.get(e.date) ?? 1).toFixed(4),
  }));
  const posCount = [
    {name:"做空",value:pos.filter(p=>p.position===-1).length,fill:DOWN_COLOR},
    {name:"觀望",value:pos.filter(p=>p.position===0).length,fill:"#4b5563"},
    {name:"做多",value:pos.filter(p=>p.position===1).length,fill:UP_COLOR},
  ];
  const alphaData = combined.map(d=>({date:d.date,alpha:+((d.strategy-d.bh)*100).toFixed(3)}));

  const axStyle = {tick:{fontSize:10,fill:"#4b5563"},axisLine:{stroke:"rgba(255,255,255,0.05)"},tickLine:{stroke:"rgba(255,255,255,0.05)"}};
  const CardBox = ({children,title,style={}})=>(
    <div className="card" style={{padding:24,...style}}>
      {title && <p style={{fontSize:11,fontWeight:600,color:"#475569",textTransform:"uppercase",letterSpacing:"0.08em",marginBottom:16}}>{title}</p>}
      {children}
    </div>
  );

  return (
    <div style={{maxWidth:1100}}>
      {/* Header */}
      <div style={{display:"flex",alignItems:"flex-start",justifyContent:"space-between",gap:12,flexWrap:"wrap",marginBottom:28}}>
        <div>
          <h1 style={{fontSize:22,fontWeight:800,color:"#f1f5f9",letterSpacing:"-0.02em"}}>📊 回測報告</h1>
          <p style={{fontSize:12,color:"#475569",marginTop:4,fontFamily:"'JetBrains Mono',monospace"}}>RUN {id?.slice(0,8).toUpperCase()}</p>
        </div>
        <div style={{display:"flex",gap:10,flexWrap:"wrap"}}>
          <button
            type="button"
            onClick={handleDownloadSourceData}
            disabled={isDownloadingSourceData}
            style={{
              padding:"9px 18px",
              borderRadius:9,
              border:"1px solid rgba(148,163,184,0.16)",
              background:"rgba(15,23,42,0.78)",
              color:"#e2e8f0",
              fontSize:13,
              fontWeight:600,
              cursor:isDownloadingSourceData?"wait":"pointer",
              opacity:isDownloadingSourceData?0.72:1,
            }}>
            {isDownloadingSourceData ? "匯出中..." : "下載原始資料 CSV"}
          </button>
          <Link to={`/run/${id}/prediction`}
            style={{padding:"9px 18px",borderRadius:9,background:"linear-gradient(135deg,#7c3aed,#6366f1)",color:"#fff",fontSize:13,fontWeight:600,textDecoration:"none"}}>
            🔮 查看明日預測 →
          </Link>
        </div>
      </div>

      {/* Metrics */}
      <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:10,marginBottom:20}}>
        <MetricCard label="策略總報酬"   value={pct(m.total_return)}   trend={m.total_return>0?"positive":"negative"} mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
        <MetricCard label="Buy & Hold"  value={pct(m.bh_return)}      trend={m.bh_return>0?"positive":"negative"}   mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
        <MetricCard label="年化報酬"     value={pct(m.annualized_ret)} trend={m.annualized_ret>0?"positive":"negative"} mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
        <MetricCard label="Sharpe Ratio" value={n(m.sharpe_ratio)}    trend={m.sharpe_ratio>0?"positive":m.sharpe_ratio<0?"negative":"neutral"} mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
        <MetricCard label="Calmar Ratio" value={n(m.calmar_ratio)}    trend={m.calmar_ratio>1?"positive":"neutral"}  mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
        <MetricCard label="最大回撤"     value={pct(m.max_drawdown)}   trend="negative" mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
        <MetricCard label="勝率"         value={pct(m.win_rate)}       trend={m.win_rate>0.5?"positive":"negative"}  mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
        <MetricCard label="換手率"       value={pct(m.turnover_rate)}  trend="neutral" mono positiveColor={UP_COLOR} negativeColor={DOWN_COLOR} neutralColor={NEUTRAL_COLOR} />
      </div>

      {/* Equity curve */}
      <CardBox title="累積資金曲線" style={{marginBottom:16}}>
        <ResponsiveContainer width="100%" height={280}>
          <ComposedChart data={combined} margin={{top:4,right:8,bottom:0,left:-15}}>
            <defs>
              <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={UP_COLOR} stopOpacity={0.15}/>
                <stop offset="95%" stopColor={UP_COLOR} stopOpacity={0}/>
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.03)"/>
            <XAxis dataKey="date" {...axStyle} interval="preserveStartEnd"/>
            <YAxis {...axStyle} tickFormatter={v=>`${v.toFixed(2)}x`}/>
            <Tooltip content={<T/>}/>
            <Legend wrapperStyle={{fontSize:11,color:"#64748b"}}/>
            <ReferenceLine y={1} stroke="rgba(255,255,255,0.08)" strokeDasharray="4 4"/>
            <Area type="monotone" dataKey="strategy" name="策略" stroke={UP_COLOR} fill="url(#sg)" strokeWidth={2} dot={false}/>
            <Line type="monotone" dataKey="bh" name="買入持有" stroke="#374151" strokeWidth={1.5} dot={false} strokeDasharray="4 4"/>
          </ComposedChart>
        </ResponsiveContainer>
      </CardBox>

      {/* Bottom row */}
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:16,marginBottom:16}}>
        {/* Drawdown */}
        <CardBox title="回撤曲線 (%)">
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={dd} margin={{top:4,right:8,bottom:0,left:-15}}>
              <defs>
                <linearGradient id="dg" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={DOWN_COLOR} stopOpacity={0.3}/>
                  <stop offset="95%" stopColor={DOWN_COLOR} stopOpacity={0}/>
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.03)"/>
              <XAxis dataKey="date" {...axStyle} interval="preserveStartEnd"/>
              <YAxis {...axStyle} tickFormatter={v=>`${v.toFixed(1)}%`}/>
              <Tooltip content={<T/>}/>
              <ReferenceLine y={0} stroke="rgba(255,255,255,0.06)"/>
              <Area type="monotone" dataKey="value" name="回撤%" stroke={DOWN_COLOR} fill="url(#dg)" strokeWidth={1.5} dot={false}/>
            </AreaChart>
          </ResponsiveContainer>
        </CardBox>

        {/* Alpha vs BH */}
        <CardBox title="每日超額報酬 vs B&H (%)">
          <ResponsiveContainer width="100%" height={180}>
            <ComposedChart data={alphaData} margin={{top:4,right:8,bottom:0,left:-15}}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.03)"/>
              <XAxis dataKey="date" {...axStyle} interval="preserveStartEnd"/>
              <YAxis {...axStyle} tickFormatter={v=>`${v.toFixed(1)}%`}/>
              <Tooltip content={<T/>}/>
              <ReferenceLine y={0} stroke="rgba(255,255,255,0.1)"/>
              <Bar dataKey="alpha" name="超額%">
                {alphaData.map((d,i)=><Cell key={i} fill={d.alpha>=0?UP_COLOR:DOWN_COLOR} opacity={0.7}/>)}
              </Bar>
            </ComposedChart>
          </ResponsiveContainer>
        </CardBox>
      </div>

      {/* Position dist */}
      <CardBox title="部位分佈">
        <div style={{display:"flex",gap:12}}>
          {posCount.map(p=>(
            <div key={p.name} style={{flex:1,background:"rgba(255,255,255,0.02)",borderRadius:10,padding:"16px 20px",textAlign:"center",border:`1px solid ${p.fill}22`}}>
              <p style={{fontSize:11,color:"#475569",textTransform:"uppercase",letterSpacing:"0.07em",marginBottom:8}}>{p.name}</p>
              <p style={{fontSize:28,fontWeight:800,color:p.fill,fontFamily:"'JetBrains Mono',monospace"}}>{p.value}</p>
              <p style={{fontSize:11,color:"#374151",marginTop:4}}>天</p>
            </div>
          ))}
        </div>
      </CardBox>
    </div>
  );
}
