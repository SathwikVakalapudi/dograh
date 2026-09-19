"use client";

import { useEffect, useRef, useState } from "react";

import {
    getModelConfigurationV2ApiV1OrganizationsModelConfigurationsV2Get,
    getModelConfigurationV2DefaultsApiV1OrganizationsModelConfigurationsV2DefaultsGet,
} from "@/client/sdk.gen";
import type {
    ModelConfigurationPricingResponse,
    OrganizationAiModelConfigurationResponse,
} from "@/client/types.gen";
import type { ModelConfigurationDefaultsV2 } from "@/components/AIModelConfigurationV2Editor";
import { detailFromError } from "@/lib/apiError";
import { fetchModelConfigurationPricing } from "@/lib/modelConfigurationPricing";

export interface UseModelConfigurationResult {
    defaults: ModelConfigurationDefaultsV2 | null;
    organizationConfiguration: OrganizationAiModelConfigurationResponse | null;
    pricing: ModelConfigurationPricingResponse | null;
    loading: boolean;
    error: string | null;
}

/**
 * Loads the data the model-override editor needs: the model configuration
 * DEFAULTS, the ORGANIZATION model configuration, and model PRICING. Shared by
 * the workflow Settings page and the in-editor Model dialog so both fetch and
 * render the model editor identically. Fetches once per mount.
 */
export function useModelConfiguration(): UseModelConfigurationResult {
    const [defaults, setDefaults] = useState<ModelConfigurationDefaultsV2 | null>(null);
    const [organizationConfiguration, setOrganizationConfiguration] =
        useState<OrganizationAiModelConfigurationResponse | null>(null);
    const [pricing, setPricing] = useState<ModelConfigurationPricingResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const hasFetched = useRef(false);

    useEffect(() => {
        if (hasFetched.current) return;
        hasFetched.current = true;

        const load = async () => {
            setLoading(true);
            setError(null);
            const [defaultsResult, configurationResult, pricingResult] = await Promise.all([
                getModelConfigurationV2DefaultsApiV1OrganizationsModelConfigurationsV2DefaultsGet(),
                getModelConfigurationV2ApiV1OrganizationsModelConfigurationsV2Get(),
                fetchModelConfigurationPricing(),
            ]);

            if (defaultsResult.error) {
                setError(detailFromError(defaultsResult.error, "Failed to load model configuration defaults"));
                setLoading(false);
                return;
            }
            if (configurationResult.error) {
                setError(detailFromError(configurationResult.error, "Failed to load model configuration"));
                setLoading(false);
                return;
            }

            setDefaults(defaultsResult.data as ModelConfigurationDefaultsV2);
            setOrganizationConfiguration(configurationResult.data || null);
            setPricing(pricingResult);
            setLoading(false);
        };

        load();
    }, []);

    return { defaults, organizationConfiguration, pricing, loading, error };
}
