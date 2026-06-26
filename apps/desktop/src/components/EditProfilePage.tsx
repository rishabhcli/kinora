import ProfileEditor from "./settings/ProfileEditor";

// Standalone Edit Profile route — the same editor surfaced in Settings ▸ Account,
// so there's one profile form, not two.
export default function EditProfilePage() {
  return (
    <div className="pt-12 pb-8 px-6 max-w-[760px] mx-auto relative z-10">
      <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-6 pt-4">Edit Profile</h1>
      <ProfileEditor />
    </div>
  );
}
