# Introduction

This report documents the qualification campaign results for the Copernicus ARD Prototype Processor (APP). It serves as the formal execution record for the test cases defined in the Software_Verification_Plan-v.1.1.  This report records the operational outcomes: test runs conducted on real SAR acquisition data, evaluated performance metrics, and final compliance verdicts.

The primary objective is to verify that the APP correctly and completely implements all functional, performance, design, and algorithmic requirements as mandated by the project's technical baseline, specifically for Analysis Ready Data (ARD) products like the Co-registered Single Look Complex (CSLC) product.

The qualification activities are organized as follows:

*   **CSLC Co-Registration and Radiometric Qualification (Sentinel-1)**: Covers test cases TC-ST-PERF-003 (model-based) and TC-ST-PERF-004 (data-driven). Point-target metrics—including Peak Side Lobe Ratio (PSLR), Integrated Side Lobe Ratio (ISLR), radiometric stability, spatial resolution, and CR co-registration precision—are verified against target specifications.
*   **Enhanced Spectral Diversity (ESD) Co-Registration Accuracy Analysis (Sentinel-1)**: Evaluates the ESD correction, verifying that residual azimuth co-registration errors are reduced to target sub-pixel thresholds when combined corrections (ETAD + ESD) are applied.
*   **Geolocation Accuracy Assessment**: Evaluates the absolute geolocation accuracy in the North and East ground components, as defined in test case TC-ST-PERF-005.
*   **End-to-End Cross-Comparison with Third-Party Processors (SAOCOM)**: Validates the CSLC processing chain for SAOCOM L-band data, benchmarking output products against independent reference implementations generated with ISCE2 and ESA SNAP (test case TC-ST-FUNC-004).

Each chapter opens with the test case specifications (objectives, input datasets, preconditions, test procedures, and pass criteria), followed by execution log analyses and summary tables detailing target thresholds and final compliance verdicts. Input datasets conform to the datasets defined in the test plan, such as Sentinel-1 and SAOCOM L-band data, with metric definitions and evaluation methods adhering to the Algorithm Theoretical Baseline Document (ATBD).
