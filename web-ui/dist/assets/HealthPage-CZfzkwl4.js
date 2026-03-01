import{b as n,j as e,a as h,A as x,L as m}from"./index-0qi2u8ik.js";import{u}from"./useHealth-D6vLzxZm.js";import{H as j}from"./HealthRing-Dpbwrvw_.js";import{M as s}from"./MetricCard-CeDyKYAP.js";import{S as p}from"./StatusBadge-BbzDpVh8.js";import{C as o,a as g,b as y,c as f}from"./Card-CJGMFJ56.js";import{T as N,a as b,b as i,c as r,d as k,e as l}from"./Table-CacWgWrm.js";import"./client-DcQdd-qf.js";import"./Badge-CquDokyO.js";import"./index-CWiSWoE0.js";/**
 * @license lucide-react v0.474.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const H=[["circle",{cx:"12",cy:"12",r:"10",key:"1mglay"}],["line",{x1:"12",x2:"12",y1:"8",y2:"12",key:"1pkeuh"}],["line",{x1:"12",x2:"12.01",y1:"16",y2:"16",key:"4dfq90"}]],_=n("CircleAlert",H);/**
 * @license lucide-react v0.474.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const A=[["circle",{cx:"12",cy:"12",r:"10",key:"1mglay"}],["path",{d:"M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3",key:"1u773s"}],["path",{d:"M12 17h.01",key:"p32p05"}]],v=n("CircleHelp",A);/**
 * @license lucide-react v0.474.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const C=[["path",{d:"M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z",key:"c3ymky"}],["path",{d:"M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27",key:"1uw2ng"}]],w=n("HeartPulse",C);/**
 * @license lucide-react v0.474.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const T=[["path",{d:"m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3",key:"wmoenq"}],["path",{d:"M12 9v4",key:"juzpu7"}],["path",{d:"M12 17h.01",key:"p32p05"}]],M=n("TriangleAlert",T);function E(){const{data:a,isLoading:d,error:c}=u();return d?e.jsx("div",{className:"flex items-center justify-center py-24",children:e.jsx(h,{size:32})}):c?e.jsxs("p",{className:"py-12 text-center text-sm text-red-500",children:["Failed to load health data: ",c.message]}):a?e.jsxs("div",{className:"space-y-6",children:[e.jsxs("div",{children:[e.jsx("h1",{className:"text-3xl font-bold tracking-tight",children:"Agent Health"}),e.jsx("p",{className:"mt-1 text-muted-foreground",children:"Heartbeat monitoring and health status for all agents."})]}),e.jsxs("div",{className:"grid gap-4 sm:grid-cols-2 lg:grid-cols-5",children:[e.jsx(s,{label:"Total Agents",value:a.total,icon:x}),e.jsx(s,{label:"Healthy",value:a.healthy,icon:w}),e.jsx(s,{label:"Warning",value:a.warning,icon:M}),e.jsx(s,{label:"Critical",value:a.critical,icon:_}),e.jsx(s,{label:"Unknown",value:a.unknown,icon:v})]}),e.jsxs("div",{className:"grid gap-6 lg:grid-cols-[auto_1fr]",children:[e.jsx(o,{className:"flex items-center justify-center p-6",children:e.jsx(j,{healthy:a.healthy,warning:a.warning,critical:a.critical,unknown:a.unknown})}),e.jsxs(o,{children:[e.jsx(g,{children:e.jsx(y,{className:"text-lg",children:"All Agents"})}),e.jsx(f,{className:"p-0",children:e.jsxs(N,{children:[e.jsx(b,{children:e.jsxs(i,{children:[e.jsx(r,{children:"Agent ID"}),e.jsx(r,{children:"Status"}),e.jsx(r,{children:"Last Heartbeat"}),e.jsx(r,{className:"text-right",children:"Missed"})]})}),e.jsxs(k,{children:[a.agents.length===0&&e.jsx(i,{children:e.jsx(l,{colSpan:4,className:"text-center text-muted-foreground",children:"No agents reporting."})}),a.agents.map(t=>e.jsxs(i,{children:[e.jsx(l,{children:e.jsx(m,{to:`/health/${t.agent_id}`,className:"font-mono text-sm text-primary hover:underline",children:t.agent_id})}),e.jsx(l,{children:e.jsx(p,{status:t.status})}),e.jsx(l,{className:"text-sm text-muted-foreground",children:t.last_heartbeat?new Date(t.last_heartbeat).toLocaleString():"--"}),e.jsx(l,{className:"text-right font-mono",children:t.missed_count})]},t.agent_id))]})]})})]})]})]}):null}export{E as default};
