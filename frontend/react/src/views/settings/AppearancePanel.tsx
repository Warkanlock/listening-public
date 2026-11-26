import { THEMES, useAppearance } from "../../appearance";
import { RoomIcon } from "../../components/RoomIcon";

export function AppearancePanel() {
  const appearance = useAppearance();
  if (!appearance) return null;
  const { theme, pending, error, select } = appearance;

  return (
    <section aria-labelledby="appearance-heading">
      <header className="settings-section-title">
        <div>
          <h2 id="appearance-heading">Appearance</h2>
          <p>Your theme follows your account.</p>
        </div>
      </header>
      <div className="appearance-options" role="group" aria-label="Theme" aria-busy={pending}>
        {THEMES.map(preset => (
          <button type="button" key={preset.id} className="appearance-option"
            aria-pressed={theme === preset.id} disabled={pending} onClick={() => void select(preset.id)}>
            <span className="appearance-preview" data-palette={preset.id} aria-hidden="true">
              <span className="appearance-preview-menu"><i /><i /><i /></span>
              <span className="appearance-preview-room">
                <i /><i />
                <span><RoomIcon name="play" /></span>
              </span>
            </span>
            <span className="appearance-option-label">
              <strong>{preset.name}</strong>
              {theme === preset.id && <RoomIcon name="check" />}
            </span>
            <span className="appearance-description">{preset.description}</span>
          </button>
        ))}
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
