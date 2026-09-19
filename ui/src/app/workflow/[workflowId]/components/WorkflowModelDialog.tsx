"use client";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { WorkflowModelOverrides } from "@/components/workflow/WorkflowModelOverrides";
import { useModelConfiguration } from "@/hooks/useModelConfiguration";
import { resolveWorkflowConfigurations, type WorkflowConfigurations } from "@/types/workflow-configurations";

interface WorkflowModelDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    workflowConfigurations: WorkflowConfigurations | null;
    workflowName: string;
    onSave: (configurations: WorkflowConfigurations, workflowName: string) => Promise<void>;
}

/**
 * In-canvas model configuration for the current agent. Opens the same
 * per-workflow model override editor that lives on the Settings page, so a user
 * can set this agent's model without leaving the workflow editor. Saving goes
 * through the editor's own `saveWorkflowConfigurations` (shared `useWorkflowState`
 * hook), which persists only the configurations (workflow_definition: null), so
 * it never touches unsaved canvas graph or the publish flow.
 */
export const WorkflowModelDialog = ({
    open,
    onOpenChange,
    workflowConfigurations,
    workflowName,
    onSave,
}: WorkflowModelDialogProps) => {
    const {
        defaults: modelConfigurationDefaults,
        organizationConfiguration: organizationModelConfiguration,
        pricing: modelConfigurationPricing,
        loading: modelConfigurationLoading,
        error: modelConfigurationError,
    } = useModelConfiguration();

    const resolvedConfigurations = workflowConfigurations
        ? resolveWorkflowConfigurations(workflowConfigurations)
        : null;

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
                <DialogHeader>
                    <DialogTitle>Model Configuration</DialogTitle>
                    <DialogDescription>
                        Configure the language, speech-to-text and text-to-speech models for this agent.
                    </DialogDescription>
                </DialogHeader>

                {resolvedConfigurations && (
                    <WorkflowModelOverrides
                        workflowConfigurations={resolvedConfigurations}
                        workflowName={workflowName}
                        onSave={onSave}
                        modelConfigurationDefaults={modelConfigurationDefaults}
                        organizationModelConfiguration={organizationModelConfiguration}
                        modelConfigurationPricing={modelConfigurationPricing}
                        modelConfigurationLoading={modelConfigurationLoading}
                        modelConfigurationError={modelConfigurationError}
                    />
                )}
            </DialogContent>
        </Dialog>
    );
};
