
import ModelConfigurationV2 from "@/components/ModelConfigurationV2";
// "Learn more" link hidden for now — restore by passing
// docsUrl={SETTINGS_DOCUMENTATION_URLS.modelOverrides} and re-importing it.

export default function ServiceConfigurationPage() {
    return (
        <div className="min-h-screen">
            <div className="container mx-auto px-4 py-8">
                <div className="max-w-4xl mx-auto">
                    <ModelConfigurationV2 />
                </div>
            </div>
        </div>
    );
}
