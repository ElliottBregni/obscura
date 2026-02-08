import { Routes, Route, Navigate } from 'react-router-dom';
import { Layout } from './components/layout/Layout';
import { Dashboard } from './features/dashboard/Dashboard';
import { AgentsList } from './features/agents/AgentsList';
import { SpawnWizard } from './features/agents/SpawnWizard';
import { MemoryBrowser } from './features/memory/MemoryBrowser';
import { WorkflowsList } from './features/workflows/WorkflowsList';
import { SkillsList } from './features/skills/SkillsList';
import { HealthDashboard } from './features/health/HealthDashboard';
import { AdminSettings } from './features/admin/AdminSettings';

function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="agents" element={<AgentsList />} />
        <Route path="agents/spawn" element={<SpawnWizard />} />
        <Route path="memory" element={<MemoryBrowser />} />
        <Route path="workflows" element={<WorkflowsList />} />
        <Route path="skills" element={<SkillsList />} />
        <Route path="health" element={<HealthDashboard />} />
        <Route path="settings" element={<AdminSettings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default App;
