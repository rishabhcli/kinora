import ProfileEditor from "./settings/ProfileEditor";

export default function EditProfilePage() {
  return (
    <div className="pt-16 pb-12 px-6 max-w-[720px] mx-auto relative z-10">
      {/* Header */}
      <div className="mb-8 pt-4">
        <p className="text-[11px] font-medium text-kinora-muted mb-2 tracking-wide uppercase">Profile</p>
        <h1 className="font-serif text-3xl font-semibold text-kinora-text">Edit Profile</h1>
        <p className="text-[13px] text-kinora-muted mt-2">
          Manage your personal information and reading preferences.
        </p>
      </div>

      {/* Editor card */}
      <div
        className="rounded-2xl p-6"
        style={{
          background: "linear-gradient(180deg, rgba(255,255,255,0.045) 0%, rgba(255,255,255,0.02) 100%)",
          border: "1px solid rgba(255,255,255,0.07)",
          boxShadow: "0 8px 32px -12px rgba(0,0,0,0.5)",
        }}
      >
        <ProfileEditor />
      </div>
    </div>
  );
}
