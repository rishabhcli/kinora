import { GeometricAvatar } from "./Navbar";

export default function EditProfilePage() {
  return (
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-6 pt-4">
        Edit Profile
      </h1>

      <div className="flex flex-col lg:flex-row gap-6">
        {/* Left: avatar */}
        <div className="lg:w-64 shrink-0">
          <div className="flex items-center gap-4 mb-2">
            <GeometricAvatar size={56} />
            <div className="min-w-0">
              <p className="text-[14px] font-semibold text-kinora-text truncate">User</p>
              <p className="text-[11px] text-kinora-muted truncate">user@kinora.app</p>
            </div>
          </div>
          <button className="text-[11px] text-kinora-muted hover:text-kinora-text transition-colors ml-1">
            Change avatar
          </button>
        </div>

        {/* Right: form */}
        <div className="flex-1 max-w-xl">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
            <div>
              <label className="block text-[11px] font-medium text-kinora-muted mb-1.5">Display Name</label>
              <input
                type="text"
                defaultValue="User"
                className="glass-input w-full px-3.5 py-2.5 rounded-xl text-[13px] text-kinora-text"
              />
            </div>
            <div>
              <label className="block text-[11px] font-medium text-kinora-muted mb-1.5">Email</label>
              <input
                type="email"
                defaultValue="user@kinora.app"
                className="glass-input w-full px-3.5 py-2.5 rounded-xl text-[13px] text-kinora-text"
              />
            </div>
          </div>
          <div className="mb-4">
            <label className="block text-[11px] font-medium text-kinora-muted mb-1.5">Bio</label>
            <textarea
              rows={3}
              placeholder="Tell us about yourself..."
              className="glass-input w-full px-3.5 py-2.5 rounded-xl text-[13px] text-kinora-text resize-none"
            />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-6">
            <div>
              <label className="block text-[11px] font-medium text-kinora-muted mb-1.5">Favorite Genre</label>
              <select className="glass-input w-full px-3.5 py-2.5 rounded-xl text-[13px] text-kinora-text" defaultValue="Fiction">
                <option style={{ background: "#161410" }}>Fiction</option>
                <option style={{ background: "#161410" }}>Non-Fiction</option>
                <option style={{ background: "#161410" }}>Mystery</option>
                <option style={{ background: "#161410" }}>Sci-Fi</option>
                <option style={{ background: "#161410" }}>Biography</option>
              </select>
            </div>
            <div>
              <label className="block text-[11px] font-medium text-kinora-muted mb-1.5">Reading Goal (books/year)</label>
              <input
                type="number"
                defaultValue={50}
                className="glass-input w-full px-3.5 py-2.5 rounded-xl text-[13px] text-kinora-text"
              />
            </div>
          </div>

          <div className="flex items-center gap-3">
            <button
              className="px-5 py-2.5 rounded-xl text-[13px] font-semibold text-kinora-text"
              style={{ background: "rgba(255, 255, 255, 0.08)" }}
            >
              Save Changes
            </button>
            <button
              className="px-5 py-2.5 rounded-xl text-[13px] font-medium text-kinora-muted"
              style={{ background: "transparent" }}
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
