import ProfileEditor from "./settings/ProfileEditor";

export default function EditProfilePage() {
  return (
    <div className="pt-16 pb-12 px-6 max-w-[720px] mx-auto relative z-10">
      {/* Header — home page style with gold accent */}
      <div className="mb-8 pt-4">
        <div className="flex items-center gap-3 mb-2">
          <span className="inline-block" style={{ width: 28, height: 2, background: "linear-gradient(90deg, #d4a44e, transparent)" }} />
          <p className="text-[10px] font-semibold uppercase tracking-[0.26em]" style={{ color: "#d4a44e" }}>Profile</p>
        </div>
        <h1 className="font-serif text-2xl font-semibold text-kinora-text">Edit Profile</h1>
        <p className="text-[12px] text-kinora-muted mt-1.5">
          Manage your personal information and reading preferences.
        </p>
      </div>

      {/* Editor card */}
      <div
        className="rounded-lg p-6"
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
