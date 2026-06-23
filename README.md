# Clippy Vision V1.0

## Motivation

When creating projects, I dont just code...I juggle between reading lots of articles, products similar to my idea, seeing documentations, seeing how to debug a code and so on. Now, when I have to ask LLM chatbots like ChatGPT or Claude about how to debug this error, or does my project idea sound good, I need to explain it eveything, what my idea is, what I have researched so far, or when debugging, what I already tried and so on. This gets more frustrating, when the conversation thread gets too long, and when you create a new session, though they dont forget about you or what you are working on, but the intermediate context is gone. And finally, if at all claude or chatGPT found a way to solve this, they would never be able to make it 100% local. Your data is not private, we all know that. Companies nowadays openly claim that they would use this data for refining the model, which is very much needed to build better models. MORE DATA = BETTER MODEL.

But, some things are needed to be kept private, for example, when you share a overly personal thing on these AI assistants, you never know if your data is being captured by some other party or not. You are leaking out information, and that is a serious privacy threat.

This is why I created Clippy Vision, it solves all the problems about context, it watches your work passively 24 by 7, so you have an assistant that knows all about you at all times. More importantly, the entire infrastructure, from database to model gateway to the LLM is all local, so no risk of data leakage. It constantly learns from you, the more you interact with it, the more the model knows you and can answer better for you, just like a journey from a stranger to a friend. But, friend might forget you, this never does that, it remembers every single detail about you and answers all your question without you explicitly providing any context.

## Tech Stack

1. Ollama: I used ollama to act as a gateway between the local LLM and Clippy Vision. I used qwen3:8b model as my main brain for this project, which handles classification, summarization, SQL generation and question answering.

I used qwen3-vl:4b model for vision classification and OCR

I also used nomic-embed-text as embedding model to convert text into vector embeddings


2. Pywin32: I used this to capture high level on-screen details on Windows OS, like the foreground window title, process name, clipboard contents, etc.

3. SQLlite: I used sqllite as it functions locally, and also is efficient enough to handle large volume of data. We store events, summaries, agent memories/facts, and user-agent conversations.

## Working/ Architecture of Clippy Vision


## Segment 1: Data Capture

This is one of the most critical segment of the entire architecture, as it builds the foundation by capturing the on-screen data and is the doorway to all other segments. 

### What is Captured?

In Version 1, we are capturing the following data:

1. Active foreground window (Title, Process Name, active urls if any)
2. Clipboard contents (Copy events, and paste events)
3. Context switch (change in foreground window)
4. Keystroke Dynamics with adaptive baseline (Key bursts, deviation from baseline)
5. screen shots (description, text from OCR is only stored in database - raw screenshots are kept on disk)

Following versions are aimed to capture more:

1. Mouse events (clicks, movement for idle detection)
2. File watcher (actively looks for modifications in file user is working)
3. On-demand continuous screen capture (screen sharing)
4. On-demand audio capture

## How is it captured

- `core/screen_capture.py`, starts the daemon, to capture and store the events.
- For each detected event, we assign an interesting score (0 to 10) and a threshold decides if the event is interesting or not.

- The event goes through a three tier classification pipeline as follows before getting stored in the database:

### 1. Tier 0: Rule based

- Here we define small rules which instantly shows high signals of whether the event is interesting or not.
- Rules:
 (a) If typing burst has fewer than 2 words OR character-to-keypress ratio < 0.30 (i.e., mostly modifier/arrow keys) --> NOT INTERESTING (score=0)
 (b) if the window switches to a known background system process (eg: msiexec.exe, SearchHost.exe) --> NOT INTERESTING (score=0)
 (c) Duplicate context change (title same even after tab switch) --> NOT INTERESTING (score=0)
 (d) Pasted clipboard content is less than 3 words --> NOT INTERESTING (score=1)
 (e) high deviation shown in typing from the baseline --> INTERESTING (score=9)

 ### 2. Tier 1: Feature based:

 - We start form a neutral score (i.e 5) and look at multiple features and provide scores for each observed feature, and finally the thresholds, (i.e INTERESTING_THRESHOLD = 7 and NOT_INTERESTING_THRESHOLD = 4 decides whether the events are interesting or not)
 - In simple language, if score > 7 --> INTERESTING,
 if score < 4 --> NOT INTERESTING

 Features observed and scoring:

 (a) If deviation in typing is detected, an is:
 (i) greater than 1.5 --> score+=2
 (ii) less than 1 --> score-=3

(b) Context novelty (How many times the process was seen in last 7 days)

(i) Never seen --> score+=2.5
(ii) seen < 5 --> score+=1.5
(iii) seen < 50 and >=5 --> score+=1
(iv) else, seen>=50 --> score+=0.5 (tiny boost)

(c) Typing intensity

- This compares current typing speed with baseline:

`wpm_z = current typing speed - mean typing speed (baseline) / standard deviation of typing speed (baseline)`

(i) if wpm_z > 1.5 --> score+=2 (unusually fast)
(ii) if wpm_z < -1.5 --> score+=1.5 (unusually slow)
(iii) else if current wpm > 0, no stable baseline --> score+=0.5 (tiny boost)

`rev_z = (current revision ratio - mean revision ratio (baseline)) / standard deviation of revision ratio`

(i) rev_z > 1.5 --> score+=1.5 (unusually high revision)
(ii) else if current revision ratio > 0.3 --> score += 0.5 (tiny boost)

(d) Clipboard/paste content length

(i) if word count > 50 --> score+=2
(ii) if word_count > 15 --> score+=1

### Tier 2: LLM classification fallback

- If the events are not able to be classified by tier0 and tier 1, then they come to tier 2, where they always recieve a classification label (interesting, not interesting, needs_vision).

- The events with score between 4 and 7 only come to tier 2

- The reason, the events that come to tier 2 recieve an ambiguous score between 4 to 7, is typically because of lack of context (as each events are individually classified by tier 0 and 1 classifiers) and strict constraints set by tier 1.

- Tier 2, solves this problem by feeding last N=3 events along with the current event to determine if it is interesting or not interesting.

- If it still feels, the events are ambiguous, then it labels the event as needs_vision, and the event is transferred to vision model for classification

### Tier 2.5: Vision Classification

- As all events, cannot be determined interesting and not interesting solely by looking at basic signals as described ago. 

- One critical problem which this tier faces, is that it cannot capture a screenshot when the tier 2 transfers the event to vision model. Becuase if it did that, it captures wrong screenshot (i.e current screen) whereas the event has already been passed. We cannot click screenshots, when each event is recorded, as it would become too expensive for storage.

- Solution is to pre-determine, which events could potentially need vision. For example, screenshots are captured when a type burst is detected (not 1, but 3 screenshots with exponential delay, to look what happens after).

- Now, we can match the timestamp of the screenshot to that when the event was recorded, and the closest screenshot is picked and given to vision model for analysis/classification. 

> **Note:** The model I used, `qwen3-vl:4b`, only supports one image per prompt, hence multiple screenshots for better context could not be done. The architecture supports the use of multiple screenshots, and other models which support more than one image can be used.


## Typing dynamics

- We record typing patterns of user in each process or app.
- This is because everyone types differently when using different apps, and capturing a single baseline typing pattern seemed less accurate. We type differently, when we are coding than when we are chatting with our friend on whatsapp/instagram.

- We store metrics like, typing speed, average dwell time, average inter-key interval (IKI), revision ratio and max pause duration.

- Alpha, that is the rate at which the baseline gets changed is set to 0.05. So whenever, new typing data comes in, it changes the baseline for that process/app with a factor of 0.05

- The baseline is used by system when we have enough samples, i.e 30 as it turned out to be a sweet spot after which deviation started to stabalize.

Deviation is calculated as follows:

```
 overall_deviation =  round(math.sqrt(sum(z**2 for z in z_scores.values()) / len(z_scores)), 2)
```

- If overall_deviation > 2.0 --> We mark it as anomaly

- In next version, I am also planning to introduce a personal baseline, which is an overall baseline, and captures more metrics. This would be used when sample size for a particular process is less than the threshold (30).

## Segment 2: Summarization

- As we saw in segment 1, there are lots of events that get recorded, and for an average user who spends 5-6 hours on PC actively working on something would easily have thousands of events or maybe more getting recorded. 

- Hence, we summarize events every 5 minutes using LLM (qwen3:8b)

- One threshold or constraint I added here on top of this is, the summarizer runs only if it has more than 3 interesting events in pipeline ready to be summarized. This is because:
A) If there are no interesting events in pipeline in 5 minute window, then there is no point in summarizing events that are not interesting

B) If there are only 1 or 2 events, it forfeits the purpose of running a summarizer.

- Another thing to note here is, the summarizer runs in two passes per tick. Pass 1 summarizes all pending events immediately, without waiting for vision classification to complete — it does not block. Pass 2 then goes back and re-summarizes any sessions where vision has since finished, overwriting the earlier summary with richer data.

- Why do we not wait for vision before summarizing?
-> If summarizer waits for the vision model to run (which takes around 40-60 seconds or more depending on device), it creates an overhead in summarization over time.
-> Tier 2 takes significantly less time to compute than the vision model, and instantly provides us with labels, creating very little overhead.
-> Hence, for instant availability of data, we summarize events without waiting for vision classification to be completed, and once vision arrives, we rewrite the summary with the new information.

## Segment 3: Distiller

- Summarizer solved the memory problem with events, but notice how summaries itself can get more in volume a few times after the events.

- To make another hierarchy of data to capture events in high level context, we have a distiller that runs every 5 sessions to extract meaningful facts/patterns to store.

- Each session is defined as following:

(i) The consecutive time between each summary should be less than 30 minutes
(ii) There should not be more than 20 summaries in one session
(iii) To be added in future versions: We break each session depending on user's activity.

- The facts extracted by distiller are stored in form of clusters. Meaning, each fact is vector embedded, and compared to the vector embedding of existing cluster centroids. If the similarity is greater than CLUSTER_THRESHOLD = 0.75, then we route it to the closest existing cluster, otherwise we create a new cluster.

- But just routing a fact to an existing cluster is not enough. Once a matching cluster is found, a second LLM call decides what to actually do with it:
(i) ADD the fact as new information, 
(ii) UPDATE an existing fact in the cluster, 
(iii) or NOOP if the fact is already captured. 
This is what keeps the memory clean and non-redundant.

- Clusters are important to merge existing facts. If in later runs, we get similar facts, they can simply suppress the older fact(s) and our memory stays clean with non-duplicate facts.

- The distiller also runs after the second pass of the summarizer (re-summarization with vision data), not just on the regular 5-session schedule.

- Major issue to address in future version: If one fact directly contradicts another fact, there is no way currently to resolve such cases.

## Segment 4: The Agent

- Another critical component of this system is the agent, which is the interaction gateway between the user and the events. 

- I developed a ReAct Agent equipped with function calling to make all of this work.

- Functions available to model:

(1) search_sessions : Generates and executes SQL queries on summaries table
(2) search_events:   Generates and executes SQL queries on events table
(3) recall_memory:   Lists all memory cluster labels and descriptions — a directory of what Clippy knows about you
(4) fetch_cluster: Fetches relvant memory facts from cluster
(5) save_identity: Saves user's autobiographical details
(6) save_note: Saves explicit infromation user asks to remember

- Components of prompt given to the model:

(1) Conversation history:

- The history of coversation is provided in two tiers:
(i) Tier 1: Always included -> last 2 rolling summaries + last 8 turns (4 full exchanges)
    - Rolling summaries: Every 5 saved messages we summarize the conversation and store the vector embeddings of the summary

(ii) Tier 2: When conversation is deep -> Tier 1 + retrieve 2 most relevant summaries

(2) User Profile

- Here we inject all the autobiographical information of the user

(3) Memory Context

- Here we embed the user query and find top_k = 8 relevant memory facts, with the threshold of MEMORY_MIN_SIM   = 0.30 and load all into the context

(4) Tool/function calling

- We explicitly ask the LLM, to call any tool, when needed to get more information. It is also equipped with a correction loop, so when sql generation fails, it retries by providing the error it got, or when it returns None or get irrelevant information, it calls more tools.

- The reAct loop is set to run for MAX_STEPS = 10, so it does not halluciante or gets stuck in a loop and provides final answer.

- One important thing to note is, when user chats with the agent, the conversation is sent to distiller to extract facts with same algorithm as stated above. If the facts clash, then agent is always given preference over the facts extracted by distiller.


## Segment 5: SQLite Database

- All the information, which the agent can access to, is stored here.

- refer to `core/storage.py` for table schemas of :
1) events : raw events (typing bursts, clipboard, window title...), 
2) sessions: summary of the events,
3) memory clusters,
4) memory_meta: Autobiographical memory, and metadata of distiller
5) memory_facts
6) conversation

and virtual tables for events and sessions to perform searching (in future versions)

We have two kinds of storages:

1) persistent memory: Never expires

- conversations, memory_clusters, memory_meta and memory facts fall under persistent memory, where the data is never deleted then can still be updated.

2) Non-persistent memory: each memory has a TTL (time-to-live) associated with them

- events and sessions have an expiry date after which they get deleted from the database
- Raw Events have TTL of 7 days and sessions have TTL of 90 days


