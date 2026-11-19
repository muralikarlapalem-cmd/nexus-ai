import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useAuthStore } from '../store/authStore'
import { ssoLogout } from '../auth/sso'
import {
  LayoutDashboard, Ticket, MessageSquare, Bot,
  LogOut, Brain, ChevronRight, BarChart3, Plug
} from 'lucide-react'
import clsx from 'clsx'

const NAV = [
  { to: '/dashboard', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/tickets',   icon: Ticket,          label: 'Tickets'   },
  { to: '/query',     icon: MessageSquare,   label: 'AI Query'  },
  { to: '/agents',    icon: Bot,             label: 'Agents'    },
  // The analytics API is closed to regular users, so the link is hidden for them
  { to: '/analytics', icon: BarChart3,       label: 'Analytics', roles: ['admin', 'agent', 'analyst'] },
  { to: '/integrations', icon: Plug,         label: 'Integrations', roles: ['admin', 'agent', 'analyst'] },
]

export default function Layout() {
  const { user, logout, sso } = useAuthStore()
  const navigate = useNavigate()

  // Keycloak users are signed out of Keycloak too, so the next sign-in asks again
  const handleLogout = async () => {
    logout()
    if (sso && await ssoLogout(sso)) return
    navigate('/login')
  }

  return (
    <div className="flex h-screen overflow-hidden bg-nexus-bg">
      {/* Sidebar */}
      <aside className="w-60 flex-shrink-0 flex flex-col border-r border-nexus-border bg-nexus-surface">
        {/* Logo */}
        <div className="flex items-center gap-3 px-5 py-5 border-b border-nexus-border">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center">
            <Brain size={16} className="text-white" />
          </div>
          <div>
            <p className="font-bold text-sm text-white tracking-wide">NEXUS AI</p>
            <p className="text-xs text-nexus-muted">IT Intelligence</p>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 py-4 space-y-1">
          {NAV.filter(n => !n.roles || n.roles.includes(user?.role)).map(({ to, icon: Icon, label }) => (
            <NavLink key={to} to={to} className={({ isActive }) =>
              clsx('flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-all',
                isActive
                  ? 'bg-indigo-600/20 text-indigo-400 border border-indigo-600/30'
                  : 'text-nexus-muted hover:text-white hover:bg-white/5')
            }>
              {({ isActive }) => <>
                <Icon size={16} />
                <span className="flex-1">{label}</span>
                {isActive && <ChevronRight size={12} className="opacity-50" />}
              </>}
            </NavLink>
          ))}
        </nav>

        {/* User */}
        <div className="px-3 py-4 border-t border-nexus-border">
          <div className="flex items-center gap-3 px-3 py-2">
            <div className="w-7 h-7 rounded-full bg-indigo-600 flex items-center justify-center text-xs font-bold text-white">
              {user?.username?.[0]?.toUpperCase() || 'U'}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-xs font-medium text-white truncate">{user?.username || 'User'}</p>
              <p className="text-xs text-nexus-muted truncate">{user?.role || ''}</p>
            </div>
            <button onClick={handleLogout} className="text-nexus-muted hover:text-red-400 transition-colors">
              <LogOut size={14} />
            </button>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
