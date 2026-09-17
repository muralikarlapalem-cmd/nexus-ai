import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { useAuthStore } from './store/authStore'
import Layout from './components/Layout'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import TicketsPage from './pages/TicketsPage'
import TicketDetailPage from './pages/TicketDetailPage'
import QueryPage from './pages/QueryPage'
import AgentMonitorPage from './pages/AgentMonitorPage'

function ProtectedRoute({ children }) {
  const token = useAuthStore(s => s.token)
  return token ? children : <Navigate to="/login" replace />
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<ProtectedRoute><Layout /></ProtectedRoute>}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard"       element={<DashboardPage />} />
          <Route path="tickets"         element={<TicketsPage />} />
          <Route path="tickets/:id"     element={<TicketDetailPage />} />
          <Route path="query"           element={<QueryPage />} />
          <Route path="agents"          element={<AgentMonitorPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
