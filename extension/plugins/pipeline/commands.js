// pipeline plugin — read-only export of a LinkedIn Recruiter project's full
// candidate pipeline (active + archived).
//
// It runs the Recruiter web app's own `talentRecruiterSearchHits` pipelineSearch
// finder in the page's MAIN world, so the request carries the live recruiter
// session (li_at cookie + the JSESSIONID-derived csrf-token) exactly like the
// app does — no cookie export, no separate auth. It only READS; it never saves,
// messages, or mutates the pipeline.
//
// Command (POST to the bridge /cmd):
//   { "type": "pipeline_fetch", "tab": <recruiterTabId>,
//     "projectId": "2036546450", "contractId": "2007643192" }
// → { ok, project_id, contract_id, counts, states, active_count, archived_count,
//     active:[{name,public_url,headline,location,industry,profile_urn}], archived:[…] }
//
// How it works (all generic — no hard-coded state ids):
//   1. talentHiringProjects candidateCounts → per-state member counts + the
//      "active" total (CANDIDATES_IN_HIRING_PROJECT_EXCEPT_ARCHIVED_AND_POTENTIAL).
//   2. pipelineSearch with an EMPTY hiring-state facet → every candidate (active
//      + archived), and the grand total.
//   3. archived total = all − active; the archived hiring-state(s) are the
//      state(s) whose member counts make up that difference (single-state is the
//      common case → the state whose count == archived total).
//   4. pipelineSearch filtered to the archived state(s) → the archived subset;
//      active = all − archived (set-differenced by profile urn).
//
// The big `DECORATION` projection is the Recruiter app's own pipelineSearch
// decoration, captured verbatim — the finder rejects trimmed projections, so we
// send it whole and just read the LinkedInMemberProfile fields we need.

const DECORATION = "(entityUrn,linkedInMemberProfileUrn~(entityUrn,anonymized,referenceUrn,memberPreferences(companySizeRange(minSize,maxSize),employmentTypes,interestedCandidateIntroductionStatement,locations,jobSeekingUrgencyLevel,geoLocations*~(standardGeoStyleName),openToNewOpportunities,titles),firstName,lastName,headline,location(displayName),profilePicture,vectorProfilePicture,numConnections,highlights,networkDistance,skills*(skillAssessmentVerified,skillAssessmentVerifiedAt,skillName,skill),canSendInMail,unlinked,educations*(school~(entityUrn,name),organizationUrn~(entityUrn,name),schoolName,degreeName,startDateOn,endDateOn),workExperience*(company~(entityUrn,name),companyName,title,startDateOn,endDateOn),privacySettings(allowConnectionsBrowse,showPremiumSubscriberIcon),viewerCompanyFollowing(followingViewerCompany,startedAt),contactInfo,industryName,publicProfileUrl,hasProfileVerifications,openToHirePreference(openToHiring,jobPostingUrns)),hiringProjectRecruitingProfile~(entityUrn,tags*,hiringContext,candidate,currentHiringProjectCandidate(entityUrn,created(time),addedToPipeline(time,actor~(profile~(entityUrn,firstName,lastName,headline,profilePicture,vectorProfilePicture,publicProfileUrl,followerCount,networkDistance,automatedActionProfile))),lastModified,hiringProject~(entityUrn,hiringProjectMetadata(hiringPipelineEnabled,state),hiringWorkflowUrn),candidateHiringState,previousCandidateHiringState,sourcingChannel~(entityUrn,channelType),sourcingChannelType,hireEntityRequestUrn~(entityUrn,status,created,requestDetailsUnion(targetPipelineStateUrn~(customName),currentAtsStageUrn~(customName)))),startFollowingCompanyAt,lastActivity~(activityType,entityUrn,performed(time,actor~(seatType,entityUrn,firstName,lastName)),performedByViewer,hiringActivityData),candidateMessageThreads*(candidate,entityUrn,lastInboxSentTime,inboxType,messageState,created(time,actor~(entityUrn,profile~(entityUrn,firstName,lastName,headline,profilePicture,vectorProfilePicture,publicProfileUrl,followerCount,networkDistance,automatedActionProfile)))),reviewNotes*,openReviewRequests*(entityUrn,owner,job,capProject,hiringProject,candidates,reviewers*~(entityUrn,state,profile~(entityUrn,firstName,lastName,headline,profilePicture,vectorProfilePicture,publicProfileUrl,followerCount,networkDistance,automatedActionProfile),seatEntitlements,seatRoles,contract,description,penaltyBoxInfo,entitlementsWithMetadata*,productRestrictions*),hiringContext,id,created,lastModified,deleted),sourcingChannelCandidates*(applyStarterInfo,candidate,created,entityUrn,hiringContext,lastModified,sourcingChannel,jobApplicationInfo(contactEmail,contactPhoneNumber,featured,jobApplication~,source),jobPostingRelevanceReasons*,addedToHiringProject,targetingQualificationMatch),sourcingChannel,notes*(candidate,childNotes*(candidate,childNotes*,content,created,entityUrn,hiringContext,lastModified,owner~(entityUrn,profile~(entityUrn,firstName,lastName,headline,profilePicture,vectorProfilePicture,publicProfileUrl,followerCount,networkDistance,automatedActionProfile),seatRoles,state),project,messageModified,message,parentNote,visibility,sourceType),content,created,entityUrn,hiringContext,lastModified,owner~(entityUrn,profile~(entityUrn,firstName,lastName,headline,profilePicture,vectorProfilePicture,publicProfileUrl,followerCount,networkDistance,automatedActionProfile),seatRoles,state),project,messageModified,message,parentNote,visibility,sourceType),resumeHiringDocumentsV2s*,companyRelevanceReasons*,contactInfo,messageUrl,candidateFeedbacks*(entityUrn,contract,company,hiringProject~(entityUrn,hiringProjectMetadata(name)),jobTitle,jobPosting~(title),requester,requesterRole,requestee~(entityUrn,firstName,lastName,headline,profilePicture,vectorProfilePicture,publicProfileUrl,followerCount,networkDistance,automatedActionProfile),candidate,feedback(relationship,note,reasonsToNotRecommendCandidate,skillFit),message,status,wouldRecommend,active,lastModified,lastRequestedTimeAt,thirdPartyReviewerProfile),candidateFeedbacksV2*(active,candidateUrn,entityUrn,feedback(relationship,note,reasonsToNotRecommendCandidate,skillFit,reviewNoteSelectedValue,wouldRecommend),hiringProject~(entityUrn,hiringProjectMetadata(name)),hiringProjectUrn,lastModified,message,requester,requestee~(entityUrn,firstName,lastName,headline,profilePicture,vectorProfilePicture,publicProfileUrl,followerCount,networkDistance,automatedActionProfile),requesterRole,status,includeLihaEval),screenerQuestionAnswers*,profileUrl,jobApplications,assessedCandidate(candidateRejectionRecord(entityUrn,reason,dispositionReasonUrn~(entityUrn,dispositionType,label,status)),featuredSkills*,rejectable,exportable,videoResponses*),profileViews*(entityUrn,seat),atsDataProviders*~,inMailCost,contractsWithActivities,candidateInsights(candidateRecommendedMatchesInsightsUrn~(positionsInsight,entityUrn)),tcrmHireIdentitiesUrns*~(entityUrn,dataProviderUrn~(entityUrn,name,onboardedExternalSystemTypes)),topChoiceJobApplicantMessage,topChoiceJobApplicantMessageEntries*(jobApplicationUrn~,message),candidateEvaluationUrn~(classification,preferredCriteriaMatchCount,preferredCriteriaCount,preferredCriteriaExplanations*,requiredCriteriaMatchCount,requiredCriteriaCount,requiredCriteriaExplanations*,summary,evaluationProcessingState,created),memberInATSInfo(connectedProjectAtsApplicationInfo(dataProviderUrn~,atsJobApplication(source),applicationStage,latestResume),silverMedalistInfos*),candidateSubscriptionUrn~(entityUrn,subscribeTitleChange,subscribeCompanyChange,subscribeActivelyHiringChange),~hiringProjectCandidatesCount(paging),~jobApplicationsCount(paging),~hireThirdPartyAssessmentCount(paging)),skillsMatchInsightUrn)";

const HITS_URL = "https://www.linkedin.com/talent/search/api/talentRecruiterSearchHits";

// The whole export runs in the page's MAIN world: one self-contained function so
// document.cookie / fetch credentials are the recruiter's. Args are passed in
// (executeScript can't close over module scope).
async function runExport(tabId, contractId, projectId, decoration) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [String(contractId), String(projectId), decoration, HITS_URL],
    func: async (C, P, DEC, HITS_URL) => {
      const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1];
      if (!csrf) return { ok: false, error: "no_jsessionid_cookie — is this a logged-in Recruiter tab?" };

      const projEnc = `urn%3Ali%3Ats_hiring_project%3A%28urn%3Ali%3Ats_contract%3A${C}%2C${P}%29`;
      const Hget = {
        "csrf-token": csrf,
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
      };
      // The finder's query is too long for a URL, so the app tunnels a GET
      // through POST: x-http-method-override:GET + x-restli-method:finder.
      const Hpost = {
        ...Hget,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-http-method-override": "GET",
        "x-restli-method": "finder",
      };

      // restli "reduced" encoding: structure stays literal, leaf values are
      // percent-encoded. The decoration is an opaque value → fully encoded,
      // INCLUDING parens (which encodeURIComponent leaves alone).
      const encDec = encodeURIComponent(DEC).replace(/\(/g, "%28").replace(/\)/g, "%29");

      const stateTuple = (id) =>
        `(value:urn%3Ali%3Ats_hiring_state%3A%28urn%3Ali%3Ats_contract%3A${C}%2C${id}%29,selected:true,negated:false,required:false)`;
      const mkQuery = (ids) =>
        "(facetSelections:List(" +
        "(type:TALENT_POOL,valuesWithSelections:List((value:isPotentialSeeker%3Atrue,selected:false,negated:false,required:false)))," +
        "(type:PIPELINE_SEARCH_SPOTLIGHT,valuesWithSelections:List((value:addedToPipelineByLiha%3Atrue,selected:false,negated:false,required:false),(value:addedToPipelineByNonLiha%3Atrue,selected:false,negated:false,required:false)))," +
        "(type:SOURCING_CHANNEL,valuesWithSelections:List())," +
        "(type:CANDIDATE_HIRING_STATE,valuesWithSelections:List(" + ids.map(stateTuple).join(",") + "))" +
        "),facets:List(),capSearchSortBy:HIRING_CANDIDATE_LAST_UPDATED_DATE,hiringProjects:List((text:export,entity:" + projEnc + ")))";
      const RP = `(hiringProject:${projEnc},doFacetCounting:false,doFacetDecoration:false)`;

      // Paginate one hiring-state-facet bucket; dedup by profile urn; keep only
      // real candidates (LinkedInMemberProfile — excludes the recruiter actor).
      const fetchBucket = async (ids) => {
        let start = 0, total = Infinity;
        const seen = new Map();
        while (start < total) {
          const body =
            "decoration=" + encDec + "&count=25&start=" + start +
            "&q=pipelineSearch&query=" + mkQuery(ids) + "&requestParams=" + RP;
          const r = await fetch(HITS_URL, { method: "POST", headers: Hpost, credentials: "include", body });
          if (!r.ok) return { error: "http_" + r.status, total: 0, people: [...seen.values()] };
          const j = await r.json();
          total = (j.data && j.data.metadata && j.data.metadata.total) || 0;
          for (const x of (j.included || [])) {
            if (x.$type !== "com.linkedin.talent.common.LinkedInMemberProfile") continue;
            if (seen.has(x.entityUrn)) continue;
            seen.set(x.entityUrn, {
              name: ((x.firstName || "") + " " + (x.lastName || "")).trim(),
              public_url: x.publicProfileUrl || null,
              headline: x.headline || null,
              location: (x.location && x.location.displayName) || null,
              industry: x.industryName || null,
              profile_urn: x.entityUrn,
            });
          }
          start += 25;
          if (start > 5000) break; // runaway guard
        }
        return { total, people: [...seen.values()] };
      };

      // 1) candidate counts → per-state counts + active total
      const ccUrl =
        `https://www.linkedin.com/talent/api/talentHiringProjects/urn%3Ali%3Ats_hiring_project%3A(urn%3Ali%3Ats_contract%3A${C}%2C${P})` +
        `?altkey=urn&decoration=%28entityUrn%2CcandidateCounts*%29`;
      const cc = await fetch(ccUrl, { headers: Hget, credentials: "include" }).then((r) => r.json());
      const counts = (cc.data && cc.data.candidateCounts) || [];
      const byState = {};
      let activeTotal = null;
      for (const x of counts) {
        if (x.type === "CANDIDATE_HIRING_STATE") {
          byState[String(x.entity).replace(/.*,(\d+)\)$/, "$1")] = x.count;
        } else if (x.type === "CANDIDATES_IN_HIRING_PROJECT_EXCEPT_ARCHIVED_AND_POTENTIAL") {
          activeTotal = x.count;
        }
      }
      const allStateIds = Object.keys(byState);

      // 2) everyone (empty hiring-state facet)
      const all = await fetchBucket([]);
      const allTotal = all.total;
      const archivedTotal = activeTotal != null ? (allTotal - activeTotal) : null;

      // 3) which hiring-state(s) are the archived/excluded ones
      let archivedStates = [];
      if (archivedTotal != null && archivedTotal > 0) {
        const exact = allStateIds.find((id) => byState[id] === archivedTotal);
        if (exact) {
          archivedStates = [exact];
        } else {
          const nz = allStateIds.filter((id) => byState[id] > 0).sort((a, b) => byState[b] - byState[a]);
          let sum = 0;
          for (const id of nz) { if (sum >= archivedTotal) break; archivedStates.push(id); sum += byState[id]; }
        }
      }
      const activeStateIds = allStateIds.filter((id) => !archivedStates.includes(id));

      // 4) archived subset explicitly; active = all − archived (by profile urn)
      const arch = archivedStates.length ? await fetchBucket(archivedStates) : { total: 0, people: [] };
      const archivedSet = new Set(arch.people.map((p) => p.profile_urn));
      const active = all.people.filter((p) => !archivedSet.has(p.profile_urn));

      return {
        ok: true,
        project_id: P,
        contract_id: C,
        counts: { active_total: activeTotal, archived_total: archivedTotal, all_total: allTotal, by_state: byState },
        states: { active: activeStateIds, archived: archivedStates },
        active_count: active.length,
        archived_count: arch.people.length,
        active,
        archived: arch.people,
      };
    },
  });
  return result || { ok: false, error: "no_result" };
}

// Per-stage snapshot: one pipelineSearch bucket per hiring-state id, so every
// returned profile is definitionally in that exact stage (no overlap, no
// per-candidate state resolution). `states` is the stage map the caller
// discovered from the project sidebar ([{id,name,archived}]); when omitted, the
// stages are discovered from candidateCounts (ids + counts only, no names) and
// the archived stage is inferred arithmetically (all − active).
async function runSnapshot(tabId, contractId, projectId, decoration, states) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [String(contractId), String(projectId), decoration, HITS_URL, states || null],
    func: async (C, P, DEC, HITS_URL, statesArg) => {
      const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)"?/) || [])[1];
      if (!csrf) return { ok: false, error: "no_jsessionid_cookie — is this a logged-in Recruiter tab?" };

      const projEnc = `urn%3Ali%3Ats_hiring_project%3A%28urn%3Ali%3Ats_contract%3A${C}%2C${P}%29`;
      const Hget = {
        "csrf-token": csrf,
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
      };
      const Hpost = {
        ...Hget,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-http-method-override": "GET",
        "x-restli-method": "finder",
      };
      const encDec = encodeURIComponent(DEC).replace(/\(/g, "%28").replace(/\)/g, "%29");

      const stateTuple = (id) =>
        `(value:urn%3Ali%3Ats_hiring_state%3A%28urn%3Ali%3Ats_contract%3A${C}%2C${id}%29,selected:true,negated:false,required:false)`;
      const mkQuery = (ids) =>
        "(facetSelections:List(" +
        "(type:TALENT_POOL,valuesWithSelections:List((value:isPotentialSeeker%3Atrue,selected:false,negated:false,required:false)))," +
        "(type:PIPELINE_SEARCH_SPOTLIGHT,valuesWithSelections:List((value:addedToPipelineByLiha%3Atrue,selected:false,negated:false,required:false),(value:addedToPipelineByNonLiha%3Atrue,selected:false,negated:false,required:false)))," +
        "(type:SOURCING_CHANNEL,valuesWithSelections:List())," +
        "(type:CANDIDATE_HIRING_STATE,valuesWithSelections:List(" + ids.map(stateTuple).join(",") + "))" +
        "),facets:List(),capSearchSortBy:HIRING_CANDIDATE_LAST_UPDATED_DATE,hiringProjects:List((text:export,entity:" + projEnc + ")))";
      const RP = `(hiringProject:${projEnc},doFacetCounting:false,doFacetDecoration:false)`;

      const fetchBucket = async (ids) => {
        let start = 0, total = Infinity;
        const seen = new Map();
        while (start < total) {
          const body =
            "decoration=" + encDec + "&count=25&start=" + start +
            "&q=pipelineSearch&query=" + mkQuery(ids) + "&requestParams=" + RP;
          const r = await fetch(HITS_URL, { method: "POST", headers: Hpost, credentials: "include", body });
          if (!r.ok) return { error: "http_" + r.status, total: 0, people: [...seen.values()] };
          const j = await r.json();
          total = (j.data && j.data.metadata && j.data.metadata.total) || 0;
          for (const x of (j.included || [])) {
            if (x.$type !== "com.linkedin.talent.common.LinkedInMemberProfile") continue;
            if (seen.has(x.entityUrn)) continue;
            seen.set(x.entityUrn, {
              name: ((x.firstName || "") + " " + (x.lastName || "")).trim(),
              public_url: x.publicProfileUrl || null,
              headline: x.headline || null,
              location: (x.location && x.location.displayName) || null,
              industry: x.industryName || null,
              profile_urn: x.entityUrn,
            });
          }
          start += 25;
          if (start > 5000) break;
        }
        return { total, people: [...seen.values()] };
      };

      // candidateCounts → per-state counts + the active total
      const ccUrl =
        `https://www.linkedin.com/talent/api/talentHiringProjects/urn%3Ali%3Ats_hiring_project%3A(urn%3Ali%3Ats_contract%3A${C}%2C${P})` +
        `?altkey=urn&decoration=%28entityUrn%2CcandidateCounts*%29`;
      const cc = await fetch(ccUrl, { headers: Hget, credentials: "include" }).then((r) => r.json());
      const byState = {};
      let activeTotal = null;
      for (const x of ((cc.data && cc.data.candidateCounts) || [])) {
        if (x.type === "CANDIDATE_HIRING_STATE") byState[String(x.entity).replace(/.*,(\d+)\)$/, "$1")] = x.count;
        else if (x.type === "CANDIDATES_IN_HIRING_PROJECT_EXCEPT_ARCHIVED_AND_POTENTIAL") activeTotal = x.count;
      }

      // resolve which stages to query + their labels
      let stages;
      if (statesArg && statesArg.length) {
        stages = statesArg.map((s) => ({ id: String(s.id), name: s.name || null, archived: !!s.archived }));
      } else {
        // no labels supplied — query every state id from candidateCounts; the
        // caller (skill) is responsible for attaching names / archived flags.
        stages = Object.keys(byState).map((id) => ({ id, name: null, archived: false }));
      }

      // one bucket per stage; skip stages the counts say are empty (saves requests)
      const out = [];
      for (const st of stages) {
        const expected = byState[st.id];
        if (expected === 0) { out.push({ ...st, count: 0, profiles: [] }); continue; }
        const b = await fetchBucket([st.id]);
        out.push({ ...st, count: b.total, profiles: b.people, ...(b.error ? { error: b.error } : {}) });
      }

      const allTotal = out.reduce((n, s) => n + (s.archived ? 0 : (s.count || 0)), 0);
      return {
        ok: true,
        project_id: P,
        contract_id: C,
        active_total: activeTotal,
        counts_by_state: byState,
        stages: out,
      };
    },
  });
  return result || { ok: false, error: "no_result" };
}

self.pluginRegistry.register({
  id: "pipeline",
  name: "LinkedIn pipeline export",
  match: ["*://*.linkedin.com/talent/*"],
  commands: {
    pipeline_fetch: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, type: "error", error: "no_tab" };
      if (!msg.projectId) return { ok: false, type: "error", error: "missing projectId" };
      const contractId = msg.contractId || "2007643192";
      try {
        const res = await runExport(tab.id, contractId, msg.projectId, DECORATION);
        return { type: "pipeline_result", ...res };
      } catch (e) {
        return { ok: false, type: "error", error: String((e && e.message) || e) };
      }
    },
    // Per-stage snapshot — the command the linkedin-state-snapshot skill drives.
    // msg.states (optional) = [{id,name,archived}] discovered from the sidebar.
    pipeline_snapshot: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, type: "error", error: "no_tab" };
      if (!msg.projectId) return { ok: false, type: "error", error: "missing projectId" };
      const contractId = msg.contractId || "2007643192";
      try {
        const res = await runSnapshot(tab.id, contractId, msg.projectId, DECORATION, msg.states);
        return { type: "pipeline_snapshot_result", ...res };
      } catch (e) {
        return { ok: false, type: "error", error: String((e && e.message) || e) };
      }
    },
  },
});
